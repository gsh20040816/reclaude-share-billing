"""增量采集：设备状态、请求流水、归属回填、价格回填。"""
from __future__ import annotations

import json

from . import store, ledger
from .sessions import serialized_account

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
    conn.execute("""INSERT OR IGNORE INTO device_bindings(org_id,rc_device_id,user_id)
        SELECT org_id,rc_device_id,MIN(user_id) FROM devices
        WHERE account_id=? AND org_id IS NOT NULL AND user_id IS NOT NULL
        GROUP BY org_id,rc_device_id HAVING COUNT(DISTINCT user_id)=1""", (account_id,))
    conn.execute("""UPDATE devices SET user_id=(SELECT user_id FROM device_bindings b
        WHERE b.org_id=devices.org_id AND b.rc_device_id=devices.rc_device_id)
        WHERE account_id=? AND EXISTS(SELECT 1 FROM device_bindings b
        WHERE b.org_id=devices.org_id AND b.rc_device_id=devices.rc_device_id)""", (account_id,))
    # API 里消失的设备：标记 missing（保留 user_id 供归因）。
    # unknown 也要一起判：它只会由历史流水创建，若一直停在 unknown 会永久占名额。
    rows = conn.execute("SELECT rc_device_id FROM devices WHERE account_id=? "
                        "AND state IN ('active','missing','unknown')", (account_id,)).fetchall()
    missing = [r["rc_device_id"] for r in rows if r["rc_device_id"] not in seen]
    for rid in missing:
        conn.execute("UPDATE devices SET state='missing' WHERE account_id=? AND rc_device_id=?",
                     (account_id, rid))
    conn.commit()
    return {"devices": len(seen), "missing": len(missing)}


def _claim_pending_logins(conn, account_id: str) -> int:
    """Only claim a fresh, unique authorization marker, never a time-only match."""
    rows = conn.execute(
        "SELECT * FROM login_states WHERE account_id=? AND status='approved_pending_device'",
        (account_id,)).fetchall()
    claimed = 0
    for row in rows:
        try:
            note = json.loads(row["note"] or "{}")
            expires = note.get("claim_until_ms", 0)
            if not isinstance(expires, (int, float)) or expires <= store.now_ms():
                conn.execute("UPDATE login_states SET status='needs_binding' WHERE state=?",
                             (row["state"],))
                continue
            name = note.get("device_name")
            marker = note.get("marker")
            before = set(note.get("before_ids", []))
            if not isinstance(marker, str) or not marker or not name or marker not in name:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        candidates = [d for d in conn.execute(
            "SELECT rc_device_id,org_id FROM devices WHERE account_id=? AND user_id IS NULL "
            "AND state='active' AND name=?", (account_id, name)).fetchall()
            if d["rc_device_id"] not in before]
        if len(candidates) != 1:
            continue
        did = candidates[0]["rc_device_id"]
        ledger.bind(conn, candidates[0]["org_id"], did, row["tg_user_id"])
        conn.execute("UPDATE login_states SET status='approved', note=? WHERE state=?",
                     (f"device_id={did}", row["state"]))
        store.event(conn, "approve_claimed", account_id=account_id, rc_device_id=did,
                    user_id=row["tg_user_id"], detail={"state": row["state"]})
        claimed += 1
    conn.commit()
    return claimed


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
    inserted = cur.rowcount == 1
    key = None
    if item.get("org_id") is not None and item.get("account_id") is not None:
        key = ledger.ensure_unit(conn, item["org_id"], item["account_id"])
        existing = conn.execute("SELECT billing_unit_id FROM requests WHERE id=?", (int(rid),)).fetchone()
        if existing["billing_unit_id"] not in (None, key):
            raise ValueError(f"请求 {rid} 的上游订阅归属冲突")
        conn.execute("UPDATE requests SET billing_unit_id=?,upstream_account_id=?,org_id=? WHERE id=?",
                     (key, int(item["account_id"]), int(item["org_id"]), int(rid)))
    conn.execute("""UPDATE requests SET user_id=COALESCE(user_id,
        (SELECT user_id FROM device_bindings b WHERE b.org_id=requests.org_id
         AND b.rc_device_id=requests.rc_device_id)) WHERE id=?""", (int(rid),))
    if inserted:
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
                SELECT b.user_id FROM device_bindings b
                WHERE b.org_id=requests.org_id AND b.rc_device_id=requests.rc_device_id)
           WHERE user_id IS NULL AND account_id=? AND created_at>=?""",
        (account_id, since_ms))
    conn.commit()
    return cur.rowcount or 0


def _register_unknown_devices(conn, account_id: str, since_ms: int) -> int:
    rows = conn.execute(
        """SELECT DISTINCT r.rc_device_id, r.org_id FROM requests r
           WHERE r.account_id=? AND r.created_at>=? AND r.rc_device_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM devices d
                             WHERE d.account_id=r.account_id AND d.rc_device_id=r.rc_device_id)""",
        (account_id, since_ms)).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO devices(account_id, rc_device_id, org_id, name, state, first_seen_at) "
            "VALUES (?,?,?,?,'unknown',?)",
            (account_id, r["rc_device_id"], r["org_id"], f"unknown-{r['rc_device_id']}", store.iso()))
    # Legacy history-only devices had no organization. Fill it only if the
    # replayed requests agree; never infer a subscription from the current org.
    conn.execute("""UPDATE devices SET org_id=(SELECT MIN(r.org_id) FROM requests r
        WHERE r.account_id=devices.account_id AND r.rc_device_id=devices.rc_device_id)
        WHERE account_id=? AND org_id IS NULL AND
        (SELECT COUNT(DISTINCT r.org_id) FROM requests r
         WHERE r.account_id=devices.account_id AND r.rc_device_id=devices.rc_device_id)=1""", (account_id,))
    conn.commit()
    return len(rows)


def _window(conn, account_id: str, since_ms=None, until_ms=None) -> tuple[int, int]:
    now = until_ms or store.now_ms()
    if since_ms is not None:
        return max(0, int(since_ms)), int(now)
    wm = conn.execute("SELECT last_synced_at FROM watermarks WHERE account_id=?",
                      (account_id,)).fetchone()
    overlap = store.get_int(conn, "sync_overlap_minutes", 10) * 60_000
    # A missing watermark means bootstrap ALL available upstream history.
    since = (wm["last_synced_at"] - overlap) if (wm and wm["last_synced_at"]) else 0
    return max(0, int(since)), int(now)


@serialized_account
def sync_account(pool, conn, account_id: str, since_ms=None, until_ms=None,
                 max_pages: int = 200, device_id: int | None = None) -> dict:
    """拉一轮：设备状态 + 请求流水 + 归因。失败抛 AccountUnavailable/rcapi.RcError。"""
    limit = store.get_int(conn, "history_page_limit", 200)
    since, until = _window(conn, account_id, since_ms, until_ms)
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    full = device_id is None and since_ms is None and until_ms is None
    progress = conn.execute("SELECT * FROM sync_progress WHERE account_id=?", (account_id,)).fetchone() if full else None
    if progress:
        since, until = progress["since_ms"], progress["until_ms"]
    inserted = updated = pages = 0
    max_id = None
    truncated = False
    with pool.session(account_id) as s:
        if device_id is None:
            try:
                _upsert_devices(conn, account_id, s.devices())
                _claim_pending_logins(conn, account_id)
                quota = s.device_quota()
                ledger.discover(conn, account_id, s.get("/api/app/orgs/"), quota)
            except Exception:                     # 设备列表失败不阻塞流水采集
                conn.rollback()
        cursor = progress["cursor"] if progress else None
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
            if full and cursor:
                conn.execute("INSERT INTO sync_progress VALUES (?,?,?,?) "
                             "ON CONFLICT(account_id) DO UPDATE SET since_ms=excluded.since_ms, "
                             "until_ms=excluded.until_ms, cursor=excluded.cursor",
                             (account_id, since, until, str(cursor)))
                conn.commit()
            if not cursor:
                break
            if pages >= max_pages:
                truncated = True

    attrib = _attribute(conn, account_id, since)
    unknown = _register_unknown_devices(conn, account_id, since)
    # 只有「从现有水位开始的整窗采集」才能推进水位。
    # 显式 since_ms（如登录后只补 5 分钟）只覆盖一小段，推进会把中间未采的流水永久跳过。
    if full:
        if truncated:
            # 还有下一页却没拉完：水位保持不动，下一轮从持久化 cursor 继续同一固定时间窗。
            # 若这里推进到 until，未拉到的旧页会被永久跳过。
            conn.execute(
                """INSERT INTO watermarks(account_id, last_synced_at, last_request_id, updated_at)
                   VALUES (?,NULL,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET
                     last_request_id=COALESCE(excluded.last_request_id, watermarks.last_request_id),
                     updated_at=excluded.updated_at""",
                (account_id, max_id, store.iso()))
            conn.commit()
            store.event(conn, "sync_truncated", account_id=account_id,
                        detail={"pages": pages, "since": since, "until": until,
                                "last_request_id": max_id})
        else:
            conn.execute("DELETE FROM sync_progress WHERE account_id=?", (account_id,))
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
            "truncated": truncated, "inserted": inserted, "updated": updated,
            "attributed": attrib, "unknown_devices": unknown, "last_request_id": max_id}


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
        except Exception as exc:                            # noqa: BLE001 - 尽力而为
            conn.rollback()
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
