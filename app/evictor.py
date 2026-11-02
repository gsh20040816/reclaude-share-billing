"""抢占式下线：闲置门槛 + 最久不用优先 + 下线前定向采集。"""
from __future__ import annotations

from . import store, syncer
from .sessions import serialized_account


class EvictionError(Exception):
    pass


def min_idle_ms(conn) -> int:
    return store.get_int(conn, "min_idle_minutes", 30) * 60_000


def candidates(conn, account_id: str, min_idle: int | None = None,
               exclude: set[int] | None = None, now: int | None = None) -> list:
    now = now or store.now_ms()
    idle = min_idle if min_idle is not None else min_idle_ms(conn)
    exclude = exclude or set()
    rows = conn.execute(
        """SELECT * FROM devices
           WHERE account_id=? AND state IN ('active','unknown') AND pinned=0
           ORDER BY COALESCE(last_used_at, 0) ASC""",
        (account_id,)).fetchall()
    out = []
    for r in rows:
        if r["rc_device_id"] in exclude:
            continue
        last = r["last_used_at"] or 0
        if last and now - last < idle:
            continue
        out.append(r)
    return out


def choose_victim(conn, account_id: str, **kw):
    cands = candidates(conn, account_id, **kw)
    return cands[0] if cands else None


def flush_device(pool, conn, account_id: str, rc_device_id: int) -> dict:
    """下线前读用量：定向拉两次（第二次直到 now），尽力而为。"""
    out = {"first": None, "second": None, "error": None}
    try:
        out["first"] = syncer.sync_device(pool, conn, account_id, rc_device_id)
        out["second"] = syncer.sync_device(pool, conn, account_id, rc_device_id,
                                           since_ms=store.now_ms() - 3_600_000)
    except Exception as exc:                     # noqa: BLE001
        out["error"] = str(exc)[:300]
    return out


@serialized_account
def evict(pool, conn, account_id: str, rc_device_id: int, *, reason: str = "manual",
          actor: str | None = None, dry_run: bool = True, force: bool = False) -> dict:
    row = conn.execute("SELECT * FROM devices WHERE account_id=? AND rc_device_id=?",
                       (account_id, rc_device_id)).fetchone()
    if row is None:
        raise EvictionError(f"设备 {rc_device_id} 不在本地库")
    if row["state"] == "revoked":
        return {"ok": True, "skipped": "已经撤销"}
    if not force and row["last_used_at"]:
        idle = store.now_ms() - row["last_used_at"]
        if idle < min_idle_ms(conn):
            raise EvictionError(
                f"设备 {rc_device_id} 最近 {idle // 60000} 分钟内有使用，"
                f"未达闲置门槛 {min_idle_ms(conn) // 60000} 分钟")

    conn.execute("UPDATE devices SET state='evicting' WHERE account_id=? AND rc_device_id=?",
                 (account_id, rc_device_id))
    conn.commit()

    flush = flush_device(pool, conn, account_id, rc_device_id)
    plan = {"account_id": account_id, "rc_device_id": rc_device_id, "reason": reason,
            "flush": flush, "dry_run": dry_run, "revoked": False}
    if dry_run:
        conn.execute("UPDATE devices SET state='active' WHERE account_id=? AND rc_device_id=? "
                     "AND state='evicting'", (account_id, rc_device_id))
        conn.commit()
        store.event(conn, "revoke_dry_run", actor=actor, account_id=account_id,
                    rc_device_id=rc_device_id, detail=plan)
        return plan

    try:
        if not force:
            _check_live_idle(pool, conn, account_id, rc_device_id)
        with pool.session(account_id) as s:
            s.revoke(rc_device_id)
    except Exception as exc:                     # noqa: BLE001
        conn.execute("UPDATE devices SET state='active' WHERE account_id=? AND rc_device_id=? "
                     "AND state='evicting'", (account_id, rc_device_id))
        conn.commit()
        store.event(conn, "revoke_error", actor=actor, account_id=account_id,
                    rc_device_id=rc_device_id, detail={"error": str(exc)[:300]})
        raise EvictionError(f"撤销失败：{str(exc)[:200]}") from exc

    conn.execute("UPDATE devices SET state='revoked' WHERE account_id=? AND rc_device_id=?",
                 (account_id, rc_device_id))
    conn.commit()
    plan["revoked"] = True
    plan["billing_gap"] = bool(flush.get("error"))
    store.event(conn, "revoke", actor=actor, account_id=account_id,
                rc_device_id=rc_device_id, user_id=row["user_id"], detail=plan)
    # 撤销后再补一轮该设备的流水（撤销后流水仍可查）
    try:
        syncer.sync_device(pool, conn, account_id, rc_device_id,
                           since_ms=store.now_ms() - 3_600_000)
    except Exception:                            # noqa: BLE001
        pass
    return plan


def _check_live_idle(pool, conn, account_id, device_id):
    """Fail closed if fresh activity cannot be established before automatic revoke."""
    with pool.session(account_id) as session:
        devices = session.devices()
    device = next((d for d in devices if int(d.get("id", -1)) == device_id), None)
    if device is None:
        raise EvictionError("设备已不在上游列表，取消自动撤销")
    last = device.get("last_used_at") or device.get("created_at")
    if not isinstance(last, (int, float)):
        raise EvictionError("无法确认设备闲置时间，取消自动撤销")
    recent = conn.execute("SELECT MAX(created_at) FROM requests WHERE account_id=? AND rc_device_id=?",
                          (account_id, device_id)).fetchone()[0]
    last = max(last, recent or 0)
    conn.execute("UPDATE devices SET last_used_at=? WHERE account_id=? AND rc_device_id=?",
                 (last, account_id, device_id))
    conn.commit()
    if store.now_ms() - last < min_idle_ms(conn):
        raise EvictionError("设备最近仍有使用，取消自动撤销")
