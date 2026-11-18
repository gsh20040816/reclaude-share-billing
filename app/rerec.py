"""Model-aware planning and approval. Called on the bot's ordered writer."""

import hashlib
import re
import secrets
from datetime import datetime

from . import activity, allocation, enroll, evictor, store
from .quota_refresh import timestamp, INTERVAL_MS

SIX_HOURS = 6 * 3600_000


def issue_token(conn, uid):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO rerec_tokens VALUES (?,?)", (token_id(token), uid))
    conn.commit()
    return token


def token_id(token):
    return hashlib.sha256(token.encode()).hexdigest()


def windows(account, model, now):
    updated = timestamp(account.get("usage_updated_at"))
    if updated is None or not 0 <= now - updated <= INTERVAL_MS:
        raise ValueError("额度快照过期")
    snap = account.get("usage_snapshot") or {}
    limits = snap.get("limits") or []
    applicable = {}
    for key, kind, label in [("five_hour", "session", "5h"), ("seven_day", "weekly_all", "全模型周")]:
        applicable[label] = snap.get(key) or next((w for w in limits if w.get("kind") == kind), {})
    for limit in limits:
        if limit.get("kind") == "weekly_scoped":
            name = ((limit.get("scope") or {}).get("model") or {}).get("display_name", "").lower()
            if not name:
                raise ValueError("专项额度缺少模型名")
            if name in model.split("-"):
                applicable[name] = limit
    if "fable" in model.split("-") and "fable" not in applicable:
        raise ValueError("缺少 Fable 专项额度")
    values = []
    full = []
    for label, w in applicable.items():
        pct = allocation.number(w.get("utilization", w.get("percent")))
        reset = timestamp(w.get("resets_at"))
        if pct is None or pct > 100 or (w.get("resets_at") and (reset is None or reset <= now)):
            raise ValueError(label + " 额度待刷新")
        values.append(pct)
        if pct == 100:
            full.append(
                label
                + " 已满"
                + (
                    "，重置 "
                    + datetime.fromtimestamp(reset / 1000).astimezone().isoformat(timespec="minutes")
                    if reset
                    else ""
                )
            )
    if full:
        raise ValueError(" / ".join(full))
    return max(values)


def releasable(candidates, target):
    claude_orgs = {c["org_id"] for c in candidates}
    for entry in candidates:
        login = entry["account_id"]
        if target["login_full"] and login != target["account_id"]:
            continue
        for device in entry["devices"]:
            org = device.get("org_id")
            if not org or device.get("revoked_at") or (target["org_full"] and org != target["org_id"]):
                continue
            platform = device.get("platform")
            if platform == "claude" or (platform is None and org in claude_orgs):
                yield login, device


def pick(rt, uid, model, source, source_device, bindings=(), skip_own=False):
    if not source:
        return allocation.choose(rt.pool, rt.conn)
    if not rt.conn.execute(
        "SELECT 1 FROM device_bindings WHERE org_id=? AND rc_device_id=? AND user_id=?",
        (source, source_device, uid),
    ).fetchone():
        raise ValueError("当前设备还没绑定到你的 Telegram 身份，请先用 /bind 登记")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", model):
        raise ValueError("模型名无效")
    candidates, reasons = allocation.inventory(rt.pool, rt.conn)
    now = store.now_ms()
    options = []
    for c in candidates:
        if c["org_id"] == source:
            continue
        try:
            pressure = windows(c["account"], model, now)
        except ValueError as e:
            reasons.append(f"{c['account_id']} {e}")
            continue
        selected = {key: c[key] for key in ("org_id", "account_id")}
        reusable = next(
            (
                d["id"]
                for d in c["devices"]
                if d.get("org_id") == c["org_id"]
                and not d.get("revoked_at")
                and d["id"] in bindings
                and rt.conn.execute(
                    "SELECT 1 FROM device_bindings WHERE org_id=? AND rc_device_id=? AND user_id=?",
                    (c["org_id"], d["id"], uid),
                ).fetchone()
            ),
            None,
        )
        if reusable:
            options.append(((-1, pressure, c["normalized_usage"]), dict(selected, reuse=reusable)))
            continue
        if c["available"]:
            options.append(((0, pressure, c["normalized_usage"]), selected))
            continue
        for login, device in releasable(candidates, c):
            if (device["org_id"], device["id"]) == (source, source_device):
                continue
            row = rt.conn.execute(
                """SELECT d.*, b.user_id AS owner FROM devices d JOIN device_bindings b
                USING (org_id,rc_device_id) WHERE d.account_id=? AND d.org_id=? AND d.rc_device_id=?
                AND d.state IN ('active','evicting') AND NOT EXISTS (SELECT 1 FROM devices p
                WHERE p.org_id=d.org_id AND p.rc_device_id=d.rc_device_id AND p.pinned!=0)""",
                (login, device["org_id"], device["id"]),
            ).fetchone()
            if row is None:
                continue
            last = activity.last_request(rt.conn, row)
            own = row["owner"] == uid
            if own and skip_own:
                continue
            guard = evictor.REQUEST_PROTECTION_MS if own else SIX_HOURS
            if last is None or now - last <= guard:
                continue
            created = timestamp(device.get("created_at"))
            if created and now - created <= evictor.REQUEST_PROTECTION_MS:
                continue
            victim = dict(
                id=device["id"],
                org=device["org_id"],
                login=login,
                own=own,
                idle_minutes=(now - last) // 60_000,
            )
            options.append(((1 if own else 2, last, pressure), dict(selected, victim=victim)))
    if not options:
        raise ValueError("没有符合条件的其他账号或可释放设备。" + "；".join(reasons))
    return min(options, key=lambda o: o[0])[1]


def handle(bot, uid, data):
    rt = bot.rt
    if not bot.allowed_chat({"id": uid, "type": "private"}, uid):
        raise ValueError("Telegram 用户已无访问权限")
    action = data["action"]
    source, device = int(data["source"]), int(data["device"])
    model = data["model"].lower()
    if action == "plan":
        return pick(
            rt, uid, model, source, device, data.get("bindings", []), data.get("skip_own", False)
        )
    if action != "approve" or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", data["state"]):
        raise ValueError("授权请求无效")
    if rt.cfg.get("dry_run", True):
        raise ValueError("服务端处于演练模式")
    state, aid, org = data["state"], data["account"], int(data["org"])
    previous = rt.conn.execute(
        "SELECT tg_user_id,account_id FROM login_states WHERE state=?", (state,)
    ).fetchone()
    if previous:
        if previous["tg_user_id"] != uid or previous["account_id"] != aid:
            raise ValueError("授权请求已占用")
        return {"approved": True}

    def check():
        # Replan across the pool: a newly free slot always wins over eviction.
        selected = pick(
            rt, uid, model, source, device, data.get("bindings", []), data.get("skip_own", False)
        )
        if selected["account_id"] != aid or selected["org_id"] != org:
            raise ValueError("席位已变化，请重新运行 rerec")
        v = selected.get("victim")
        agreed = data.get("victim") or {}
        if v and any(v[k] != agreed.get(k) for k in ("login", "org", "id", "own")):
            raise ValueError("待释放设备已变化，请重新选择")
        return selected

    selected = check()
    if selected.get("reuse"):
        raise ValueError("本机已有可用绑定，请重新选择")
    victim = selected.get("victim")
    if victim:
        # Validate the upstream login before doing anything irreversible.
        info = enroll.describe(rt.pool, rt.conn, state, aid)
        expires = timestamp(info.get("expires_at"))
        if not expires or expires <= store.now_ms():
            raise ValueError("授权已过期")
        evictor.evict(
            rt.pool,
            rt.conn,
            victim["login"],
            victim["id"],
            reason="rerec_switch",
            dry_run=False,
            actor=f"tg:{uid}",
            before_revoke=lambda: check().get("victim"),
        )
        selected = check()
        if selected.get("victim"):
            raise ValueError("释放后上游还没有可用名额，请稍后重试")
    enroll.enroll(
        rt.pool,
        rt.conn,
        state=state,
        tg_user_id=uid,
        device_name="",
        dry_run=False,
        actor=f"tg:{uid}",
        selected=selected,
        max_wait=0,
    )
    return {"approved": True}
