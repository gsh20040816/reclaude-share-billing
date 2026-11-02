"""开通：用户提交 reclaude login 授权链接 → 选账号 → 批准 → 登记 device_id。

批准后设备通常几秒内出现在账号的设备列表，此时按 (账号, name, user_id IS NULL,
rc_device_id 不在批准前集合) 精确匹配并绑定。若 max_wait 内设备仍未出现，就写一条
login_states(status='approved_pending_device', note=JSON{device_name, approved_at_ms})；
后续 syncer.sync_account 里的 _claim_pending_logins 会在设备出现时自动认领并回填归因。
"""
from __future__ import annotations

import json
import time

from . import evictor, store


class NoSlot(Exception):
    """所有账号都满，且没有满足闲置门槛的设备可以下线。"""


class EnrollError(Exception):
    pass


def used_slots(conn, account_id: str) -> int:
    # unknown/missing 只是历史流水或已从 API 消失的残留，不占真实席位。
    row = conn.execute(
        "SELECT COUNT(*) n FROM devices WHERE account_id=? AND state IN ('active','evicting')",
        (account_id,)).fetchone()
    return row["n"]


def account_capacity(conn, account_id: str) -> int:
    row = store.account_row(conn, account_id)
    return int((row["capacity"] if row and row["capacity"] else 4))


def free_account(conn):
    """可用账号：启用、状态 ok、名额有余。按已用席位升序（尽量均衡）。"""
    best = None
    for acc in store.enabled_accounts(conn):
        if acc["status"] not in ("ok", None):
            continue
        used, cap = used_slots(conn, acc["id"]), account_capacity(conn, acc["id"])
        if used >= cap:
            continue
        if best is None or used < best[1]:
            best = (acc, used)
    return best[0] if best else None


def pick_account(conn, preferred: str | None = None, exclude: set[int] | None = None):
    """-> (account_row, victim_row|None)。victim 不为空表示需要先下线。

    preferred 是硬约束：只允许用这一个账号，不能回退到账号池里的其他账号。
    """
    accounts = [a for a in store.enabled_accounts(conn) if a["status"] in ("ok", None)]
    if preferred:
        accounts = [a for a in accounts if a["id"] == preferred]
        if not accounts:
            return None, None
        acc = accounts[0]
        if used_slots(conn, acc["id"]) < account_capacity(conn, acc["id"]):
            return acc, None
        victim = evictor.choose_victim(conn, acc["id"], exclude=exclude)
        return (acc, victim) if victim is not None else (None, None)
    free = free_account(conn)
    if free:
        return free, None
    # 没有空位 → 找一台可下线的
    for acc in accounts:
        victim = evictor.choose_victim(conn, acc["id"], exclude=exclude)
        if victim is not None:
            return acc, victim
    return None, None


def describe(pool, conn, state: str, account_id: str) -> dict:
    with pool.session(account_id) as s:
        return s.post("/api/cli/auth/describe", {"state": state})


def _new_device_id(conn, account_id: str, before: set[int], device_name: str) -> int | None:
    """只认这次批准创建的那台设备：同账号、未绑定、批准前不存在。

    API 返回的 name 不一定等于 approve 传入的 device_name（实测可能被改写成
    `reclaude on <hostname>`），所以：只有一个新设备时直接认；出现多个才用
    name 精确匹配消歧。仍有歧义就返回 None，交给 pending 自动认领。
    """
    rows = conn.execute(
        """SELECT rc_device_id, name FROM devices
           WHERE account_id=? AND user_id IS NULL""", (account_id,)).fetchall()
    new = {int(r["rc_device_id"]) for r in rows if int(r["rc_device_id"]) not in before}
    if len(new) == 1:
        return new.pop()
    named = {int(r["rc_device_id"]) for r in rows
             if int(r["rc_device_id"]) in new and (r["name"] or "") == device_name}
    return named.pop() if len(named) == 1 else None


def enroll(pool, conn, *, state: str, tg_user_id: int, device_name: str,
           dry_run: bool = True, preferred_account: str | None = None,
           actor: str | None = None, max_wait: float = 30.0) -> dict:
    from . import syncer

    acc, victim = pick_account(conn, preferred_account)
    if acc is None:
        raise NoSlot("所有已启用账号都满员，且没有满足闲置门槛的设备可下线")

    info = describe(pool, conn, state, acc["id"])
    org_id = acc["own_org_id"] or (info.get("suggested_org_id"))
    plan = {
        "account_id": acc["id"], "org_id": org_id, "device_name": device_name,
        "hostname": info.get("hostname"), "os": info.get("os"), "arch": info.get("arch"),
        "expires_at": info.get("expires_at"), "victim": victim["rc_device_id"] if victim else None,
        "dry_run": dry_run, "device_id": None, "pending": False,
    }
    if victim is not None:
        plan["evict"] = evictor.evict(pool, conn, acc["id"], victim["rc_device_id"],
                                      reason=f"make room for {device_name}", actor=actor,
                                      dry_run=dry_run)

    if dry_run:
        store.event(conn, "enroll_dry_run", actor=actor, account_id=acc["id"], detail=plan)
        return plan

    before = {r["rc_device_id"] for r in conn.execute(
        "SELECT rc_device_id FROM devices WHERE account_id=?", (acc["id"],)).fetchall()}
    with pool.session(acc["id"]) as s:
        s.post("/api/cli/auth/approve",
               {"state": state, "device_name": device_name, "org_id": org_id})

    # 等设备出现并登记
    new_id = None
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            syncer.sync_account(pool, conn, acc["id"], since_ms=store.now_ms() - 5 * 60_000)
        except Exception:                       # noqa: BLE001
            pass
        new_id = _new_device_id(conn, acc["id"], before, device_name)
        if new_id:
            break
        time.sleep(3)

    if new_id is None:
        # 批准成功但设备还没出现：留一条 login_state（JSON 记录设备名），
        # 后续采集会调用 _claim_pending_logins 自动认领，不需要管理员手工 /bind。
        pending_note = json.dumps(
            {"device_name": device_name, "approved_at_ms": store.now_ms()},
            ensure_ascii=False)
        conn.execute(
            """INSERT INTO login_states(state, tg_user_id, account_id, status, hostname, os, arch,
                                        client_ip, expires_at, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(state) DO UPDATE SET status=excluded.status, account_id=excluded.account_id,
                 note=excluded.note""",
            (state, tg_user_id, acc["id"], "approved_pending_device", info.get("hostname"),
             info.get("os"), info.get("arch"), info.get("client_ip_masked"),
             info.get("expires_at"), store.iso(), pending_note))
        conn.commit()
        plan["device_id"] = None
        plan["pending"] = True
        store.event(conn, "approve", actor=actor, account_id=acc["id"], user_id=tg_user_id, detail=plan)
        return plan

    conn.execute(
        """INSERT INTO devices(account_id, rc_device_id, user_id, name, state, first_seen_at)
           VALUES (?,?,?,?, 'active', ?)
           ON CONFLICT(account_id, rc_device_id) DO UPDATE SET user_id=excluded.user_id""",
        (acc["id"], new_id, tg_user_id, device_name, store.iso()))
    conn.execute(
        """INSERT INTO login_states(state, tg_user_id, account_id, status, hostname, os, arch,
                                    client_ip, expires_at, created_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(state) DO UPDATE SET status=excluded.status, account_id=excluded.account_id,
             note=excluded.note""",
        (state, tg_user_id, acc["id"], "approved", info.get("hostname"), info.get("os"),
         info.get("arch"), info.get("client_ip_masked"), info.get("expires_at"), store.iso(),
         f"device_id={new_id}"))
    conn.commit()
    plan["device_id"] = new_id
    store.event(conn, "approve", actor=actor, account_id=acc["id"], rc_device_id=new_id,
                user_id=tg_user_id, detail=plan)
    return plan


def bind_device(conn, account_id: str, rc_device_id: int, user_id: int | None) -> int:
    """把已知 device_id 绑到用户（含已撤销设备的历史归因）。"""
    cur = conn.execute("UPDATE devices SET user_id=? WHERE account_id=? AND rc_device_id=?",
                       (user_id, account_id, rc_device_id))
    conn.execute("UPDATE requests SET user_id=? WHERE account_id=? AND rc_device_id=?",
                 (user_id, account_id, rc_device_id))
    conn.commit()
    return cur.rowcount or 0
