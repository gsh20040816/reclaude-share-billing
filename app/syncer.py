"""增量采集：设备状态、请求流水、归属回填、价格回填。"""
from __future__ import annotations

import time

from . import store
from .sessions import AccountUnavailable

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_create_5m_tokens",
                "cache_create_1h_tokens", "cache_read_input_tokens", "total_tokens")


def _upsert_devices(conn, account_id: str, devices: list[dict]) -> dict:
    seen = set()
    now = store.iso()
    for d in devices:
        rid = d.get("id")
        if rid is None:
            continue
        seen.add(int(rid))
        machine = d.get("machine_summary") or {}
        platform = d.get("client_platform") or machine.get("os_version")
        conn.execute(
            """INSERT INTO devices(account_id, rc_device_id, name, sk_prefix, org_id, platform,
                                   client_version, first_seen_at, last_used_at, state)
               VALUES (?,?,?,?,?,?,?,?,?,'active')
               ON CONFLICT(account_id, rc_device_id) DO UPDATE SET
                 name=excluded.name, sk_prefix=excluded.sk_prefix, org_id=excluded.org_id,
                 platform=excluded.platform, client_version=excluded.client_version,
                 last_used_at=excluded.last_used_at,
                 state=CASE WHEN devices.state='revoked' THEN 'revoked' ELSE 'active' END""",
            (account_id, int(rid), d.get("name"), d.get("sk_prefix"), d.get("org_id"),
             platform, d.get("client_version"), now, d.get("last_used_at")))
    # API 里消失的设备：标记 missing（保留 user_id 供归因）
    rows = conn.execute("SELECT rc_device_id FROM devices WHERE account_id=? AND state IN ('active','missing')",
                        (account_id,)).fetchall()
    missing = [r["rc_device_id"] for r in rows if r["rc_device_id"] not in seen]
    for rid in missing:
        conn.execute("UPDATE devices SET state='missing' WHERE account_id=? AND rc_device_id=?",
                     (account_id, rid))
    conn.commit()
    return {"devices": len(seen), "missing": len(missing)}


def _upsert_request(conn, account_id: str, item: dict) -> bool:
    """-> True 表示新插入；已存在则只刷新定价相关字段。"""
    rid = item.get("id")
    if rid is None:
        return False
    priced = item.get("pricing_status") == "priced" and item.get("cost_total_usd") is not None
    cost = float(item["cost_total_usd"]) if priced else None
    cur = conn.execute(
        """INSERT OR IGNORE INTO requests(id, account_id, rc_device_id, org_id, created_at, model,
                                request_kind, status, is_stream, duration_ms, input_tokens,
                                output_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
                                cache_read_input_tokens, total_tokens, cost_usd, pricing_status,
                                deduction_status, tracking_id, first_seen_at, priced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(rid), account_id, item.get("device_id"), item.get("org_id"), item.get("created_at"),
         item.get("model"), item.get("request_kind"), item.get("status"),
         1 if item.get("is_stream") else 0, item.get("duration_ms"),
         *(item.get(f) for f in TOKEN_FIELDS),
         cost, item.get("pricing_status"), item.get("deduction_status"),
         item.get("tracking_id"), store.iso(), store.iso() if cost is not None else None))
    if cur.rowcount == 1:
        return True
    # 已存在：只在定价状态变化时更新（幂等，不覆盖 user_id）
    conn.execute(
        """UPDATE requests SET cost_usd=?, pricing_status=?, deduction_status=?,
                  priced_at=CASE WHEN ? IS NOT NULL THEN ? ELSE priced_at END
           WHERE id=? AND (pricing_status IS NOT ? OR cost_usd IS NOT ?)""",
        (cost, item.get("pricing_status"), item.get("deduction_status"), cost, store.iso(),
         int(rid), item.get("pricing_status"), cost))
    return False




def _attribute(conn, account_id: str, since_ms: int) -> int:
    cur = conn.execute(
        """UPDATE requests SET user_id = (
                SELECT d.user_id FROM devices d
                WHERE d.account_id=requests.account_id AND d.rc_device_id=requests.rc_device_id)
           WHERE user_id IS NULL AND account_id=? AND created_at>=?""",
        (account_id, since_ms))
    conn.commit()
    return cur.rowcount or 0


def _register_unknown_devices(conn, account_id: str, since_ms: int) -> int:
    rows = conn.execute(
        """SELECT DISTINCT r.rc_device_id, r.id FROM requests r
           WHERE r.account_id=? AND r.created_at>=? AND r.rc_device_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM devices d
                             WHERE d.account_id=r.account_id AND d.rc_device_id=r.rc_device_id)""",
        (account_id, since_ms)).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO devices(account_id, rc_device_id, name, state, first_seen_at) "
            "VALUES (?,?,?,'unknown',?)",
            (account_id, r["rc_device_id"], f"unknown-{r['rc_device_id']}", store.iso()))
    conn.commit()
    return len(rows)


def _window(conn, account_id: str, since_ms=None, until_ms=None) -> tuple[int, int]:
    now = until_ms or store.now_ms()
    if since_ms is not None:
        return max(0, int(since_ms)), int(now)
    wm = conn.execute("SELECT last_synced_at FROM watermarks WHERE account_id=?",
                      (account_id,)).fetchone()
    overlap = store.get_int(conn, "sync_overlap_minutes", 10) * 60_000
    backfill = store.get_int(conn, "backfill_max_hours", 24) * 3_600_000
    since = (wm["last_synced_at"] - overlap) if (wm and wm["last_synced_at"]) else (now - backfill)
    return max(0, int(since)), int(now)


def sync_account(pool, conn, account_id: str, since_ms=None, until_ms=None,
                 max_pages: int = 200, device_id: int | None = None) -> dict:
    """拉一轮：设备状态 + 请求流水 + 归因。失败抛 AccountUnavailable/rcapi.RcError。"""
    limit = store.get_int(conn, "history_page_limit", 200)
    since, until = _window(conn, account_id, since_ms, until_ms)
    inserted = updated = pages = 0
    max_id = None
    with pool.session(account_id) as s:
        if device_id is None:
            try:
                _upsert_devices(conn, account_id, s.devices())
                quota = s.device_quota()
                conn.execute("UPDATE rc_accounts SET capacity=? WHERE id=?",
                             (quota.get("max"), account_id))
                conn.commit()
            except Exception:                     # 设备列表失败不阻塞流水采集
                pass
        cursor = None
        while pages < max_pages:
            page = s.history(since=since, until=until, cursor=cursor, limit=limit,
                             device_id=device_id)
            items = page.get("items") or []
            pages += 1
            for it in items:
                rid = it.get("id")
                if rid is None:
                    continue
                if _upsert_request(conn, account_id, it):
                    inserted += 1
                else:
                    updated += 1
                max_id = max(max_id or 0, int(rid))
            conn.commit()
            cursor = page.get("next_cursor")
            if not cursor or not items:
                break

    attrib = _attribute(conn, account_id, since)
    unknown = _register_unknown_devices(conn, account_id, since)
    if device_id is None:
        conn.execute(
            """INSERT INTO watermarks(account_id, last_synced_at, last_request_id, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 last_synced_at=excluded.last_synced_at,
                 last_request_id=COALESCE(excluded.last_request_id, watermarks.last_request_id),
                 updated_at=excluded.updated_at""",
            (account_id, until, max_id, store.iso()))
        conn.commit()
    return {"account_id": account_id, "since": since, "until": until, "pages": pages,
            "inserted": inserted, "updated": updated, "attributed": attrib,
            "unknown_devices": unknown, "last_request_id": max_id}


def backfill_prices(pool, conn, account_id: str, hours: int | None = None) -> dict:
    """把 pending 的请求重新拉一遍补价格（同一批 id 的状态更新，不是重复计费）。"""
    hours = hours or store.get_int(conn, "price_backfill_hours", 24)
    since = store.now_ms() - hours * 3_600_000
    rows = conn.execute(
        """SELECT id, created_at FROM requests
           WHERE account_id=? AND created_at>=?
             AND (pricing_status='pending' OR pricing_status IS NULL)
           ORDER BY created_at LIMIT 2000""", (account_id, since)).fetchall()
    if not rows:
        return {"pending": 0, "priced": 0}
    lo = min(r["created_at"] for r in rows) - 1000
    hi = max(r["created_at"] for r in rows) + 60_000
    want = {r["id"] for r in rows}
    priced = 0
    cursor = None
    pages = 0
    with pool.session(account_id) as s:
        while pages < 50:
            page = s.history(since=lo, until=hi, cursor=cursor, limit=200)
            items = page.get("items") or []
            pages += 1
            for it in items:
                if it.get("id") in want:
                    _upsert_request(conn, account_id, it)
                    if it.get("pricing_status") == "priced" and it.get("cost_total_usd") is not None:
                        priced += 1
            conn.commit()
            cursor = page.get("next_cursor")
            if not cursor or not items:
                break
    return {"pending": len(want), "priced": priced, "pages": pages}


def sync_all(pool, conn, only: list[str] | None = None) -> list[dict]:
    results = []
    for acc in store.enabled_accounts(conn):
        if only and acc["id"] not in only:
            continue
        try:
            r = sync_account(pool, conn, acc["id"])
            r["priced_after"] = backfill_prices(pool, conn, acc["id"])
            results.append(r)
        except (AccountUnavailable, Exception) as exc:      # noqa: BLE001 - 尽力而为
            conn.execute("UPDATE rc_accounts SET last_error=? WHERE id=?", (str(exc)[:400], acc["id"]))
            conn.commit()
            store.event(conn, "sync_error", account_id=acc["id"], detail={"error": str(exc)[:500]})
            results.append({"account_id": acc["id"], "error": str(exc)})
    return results


def sync_device(pool, conn, account_id: str, rc_device_id: int, since_ms=None) -> dict:
    """下线前定向采集：只拉这一台设备的流水。"""
    if since_ms is None:
        row = conn.execute(
            """SELECT MIN(created_at) t FROM requests
               WHERE account_id=? AND rc_device_id=?""", (account_id, rc_device_id)).fetchone()
        since_ms = row["t"] or (store.now_ms() - 30 * 86_400_000)
    return sync_account(pool, conn, account_id, since_ms=since_ms,
                        until_ms=store.now_ms(), max_pages=50, device_id=rc_device_id)
