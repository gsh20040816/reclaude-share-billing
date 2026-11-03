"""抢占式下线：闲置门槛 + 最久不用优先 + 下线前定向采集。"""
from __future__ import annotations

from . import store, syncer, activity
from .sessions import serialized_account


REQUEST_PROTECTION_MS = 90 * 60_000


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
        request = activity.last_request(conn, r)
        if request is not None and now-request <= REQUEST_PROTECTION_MS:
            continue
        upstream = r['last_used_at']
        if upstream and now-upstream >= idle:
            out.append((0, upstream, r))
        elif request is not None and now-request > max(idle, REQUEST_PROTECTION_MS):
            out.append((1, request, r))
    return [r for _, _, r in sorted(out, key=lambda item: (item[0], item[1], item[2]['rc_device_id']))]


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
    manual_override = force and reason in ('manual', 'admin')
    if not manual_override:
        _check_request_guard(conn, row)

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
        if not manual_override:
            if flush.get('error') or any((flush.get(k) or {}).get('truncated') for k in ('first', 'second')):
                raise EvictionError('请求同步未完成，取消自动撤销')
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
    row = conn.execute('SELECT * FROM devices WHERE account_id=? AND rc_device_id=?',
                       (account_id, device_id)).fetchone()
    if device.get('org_id') is not None and row['org_id'] is not None and device['org_id'] != row['org_id']:
        raise EvictionError('设备组织发生变化，取消自动撤销')
    request = activity.last_request(conn, row)
    _check_request_guard(conn, row)
    upstream = device.get('last_used_at')
    now = store.now_ms()
    # Heartbeats affect first-choice ranking, not the real-request protection.
    upstream_idle = isinstance(upstream, (int, float)) and upstream > 0 and now-upstream >= min_idle_ms(conn)
    request_idle = request is not None and now-request > max(min_idle_ms(conn), REQUEST_PROTECTION_MS)
    created = device.get('created_at')
    if isinstance(created, (int, float)) and now-created <= REQUEST_PROTECTION_MS:
        raise EvictionError('设备刚创建，取消自动撤销')
    if not upstream_idle and not request_idle:
        raise EvictionError('设备尚未满足闲置条件')
    conn.execute("UPDATE devices SET last_used_at=? WHERE account_id=? AND rc_device_id=?",
                 (upstream, account_id, device_id))
    conn.commit()


def _check_request_guard(conn, device):
    request = activity.last_request(conn, device)
    if request is not None and store.now_ms()-request <= REQUEST_PROTECTION_MS:
        raise EvictionError('最近 90 分钟内有请求，不能自动下线')
