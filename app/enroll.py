"""批准登录并用每次授权的唯一设备名标记认领；无法确认时保留人工绑定。"""
from __future__ import annotations

import json
import secrets
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

    marker = "rcb-" + secrets.token_hex(12)
    # Keep the authorization marker intact even if a display name is unusually long.
    assigned_name = f"{marker} {device_name[:32]}"
    with pool.session(acc["id"]) as session:
        live = session.devices()
        syncer._upsert_devices(conn, acc["id"], live)
        before = [int(d["id"]) for d in live if d.get("id") is not None]
        approved_at = store.now_ms()
        session.post("/api/cli/auth/approve",
                     {"state": state, "device_name": assigned_name, "org_id": org_id})
        note = json.dumps({"device_name": assigned_name, "marker": marker,
                           "before_ids": before, "approved_at_ms": approved_at,
                           "claim_until_ms": approved_at + 10 * 60_000})
        conn.execute(
            """INSERT INTO login_states(state, tg_user_id, account_id, status, hostname,
                   os, arch, client_ip, expires_at, created_at, note)
               VALUES (?,?,?,'approved_pending_device',?,?,?,?,?,?,?)
               ON CONFLICT(state) DO UPDATE SET tg_user_id=excluded.tg_user_id,
                 account_id=excluded.account_id, status=excluded.status,
                 created_at=excluded.created_at, note=excluded.note""",
            (state, tg_user_id, acc["id"], info.get("hostname"), info.get("os"),
             info.get("arch"), info.get("client_ip_masked"), info.get("expires_at"),
             store.iso(), note))
        conn.commit()

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            # Device discovery must not touch the history watermark.
            with pool.session(acc["id"]) as session:
                syncer._upsert_devices(conn, acc["id"], session.devices())
                syncer._claim_pending_logins(conn, acc["id"])
        except Exception:
            conn.rollback()
        row = conn.execute("SELECT status, note FROM login_states WHERE state=?", (state,)).fetchone()
        if row["status"] == "approved":
            plan["device_id"] = int(row["note"].split("=", 1)[1])
            break
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    plan["pending"] = plan["device_id"] is None
    store.event(conn, "approve", actor=actor, account_id=acc["id"],
                rc_device_id=plan["device_id"], user_id=tg_user_id, detail=plan)
    return plan


def bind_device(conn, account_id: str, rc_device_id: int, user_id: int | None) -> int:
    """把已知 device_id 绑到用户（含已撤销设备的历史归因）。"""
    cur = conn.execute("UPDATE devices SET user_id=? WHERE account_id=? AND rc_device_id=?",
                       (user_id, account_id, rc_device_id))
    conn.execute("UPDATE requests SET user_id=? WHERE account_id=? AND rc_device_id=?",
                 (user_id, account_id, rc_device_id))
    if cur.rowcount and user_id is not None:
        pending = conn.execute(
            "SELECT state FROM login_states WHERE account_id=? AND tg_user_id=? "
            "AND status IN ('approved_pending_device','needs_binding')",
            (account_id, user_id)).fetchall()
        # Multiple pending authorizations cannot be disambiguated by /bind alone.
        if len(pending) == 1:
            conn.execute("UPDATE login_states SET status='approved', note=? WHERE state=?",
                         (f"device_id={rc_device_id}", pending[0]["state"]))
    conn.commit()
    return cur.rowcount or 0
