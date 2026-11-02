"""开通：用户提交 reclaude login 授权链接 → 选账号 → 批准 → 登记 device_id。"""
from __future__ import annotations

import time

from . import evictor, store


class NoSlot(Exception):
    """所有账号都满，且没有满足闲置门槛的设备可以下线。"""


class EnrollError(Exception):
    pass


def used_slots(conn, account_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) n FROM devices WHERE account_id=? AND state IN ('active','unknown','evicting')",
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
    """-> (account_row, victim_row|None)。victim 不为空表示需要先下线。"""
    accounts = [a for a in store.enabled_accounts(conn) if a["status"] in ("ok", None)]
    if preferred:
        accounts = [a for a in accounts if a["id"] == preferred] or accounts
    free = free_account(conn)
    if free and (not preferred or free["id"] == preferred or True):
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


def _new_device_id(conn, account_id: str, before: set[int]) -> int | None:
    rows = conn.execute("SELECT rc_device_id FROM devices WHERE account_id=?", (account_id,)).fetchall()
    for r in rows:
        if r["rc_device_id"] not in before:
            return int(r["rc_device_id"])
    return None


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
        new_id = _new_device_id(conn, acc["id"], before)
        if new_id:
            break
        time.sleep(3)

    if new_id is None:
        # 批准成功但设备还没出现：留一条 login_state，等下一轮采集自动认领
        conn.execute(
            """INSERT INTO login_states(state, tg_user_id, account_id, status, hostname, os, arch,
                                        client_ip, expires_at, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(state) DO UPDATE SET status=excluded.status, account_id=excluded.account_id,
                 note=excluded.note""",
            (state, tg_user_id, acc["id"], "approved_pending_device", info.get("hostname"),
             info.get("os"), info.get("arch"), info.get("client_ip_masked"),
             info.get("expires_at"), store.iso(), "设备尚未出现在列表，等采集认领"))
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
           ON CONFLICT(state) DO UPDATE SET status=excluded.status, account_id=excluded.account_id""",
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
