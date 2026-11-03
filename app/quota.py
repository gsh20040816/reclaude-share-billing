"""Quota snapshots and window usage estimates; never used as billing costs."""
from datetime import datetime
from . import store, billing, syncer
from .allocation import number


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 0 else None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(dt.timestamp()*1000) if dt.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def summarize(conn, login_id, org, now=None):
    now = now or store.now_ms()
    account = org.get('current_account') or {}
    snapshot = account.get('usage_snapshot') or {}
    observed = timestamp(account.get('usage_updated_at'))
    watermark = conn.execute('SELECT last_synced_at FROM watermarks WHERE account_id=?', (login_id,)).fetchone()
    pending = conn.execute('SELECT 1 FROM sync_progress WHERE account_id=?', (login_id,)).fetchone()
    rates = billing.load_rates(conn)
    result = []
    for key, label, duration in [('five_hour','5 小时',5*3600000),('seven_day','7 天',7*86400000)]:
        window = snapshot.get(key) or {}
        pct, end = number(window.get('utilization')), timestamp(window.get('resets_at'))
        start = end-duration if end else None
        row = dict(label=label, percent=pct, start=start, end=end, observed=observed,
                   requests=0, tokens=0, usd=0, raw_usd=0, estimate=None, remaining=None, unpriced=0, reasons=[])
        if start is None or observed is None or not start <= observed < end:
            row['reasons'].append('窗口或快照时间缺失/不匹配')
            result.append(row);continue
        usage = conn.execute("""SELECT model, COUNT(*) n,COALESCE(SUM(cost_usd),0) usd,
            COALESCE(SUM(total_tokens),0) tokens,SUM(cost_usd IS NULL) unpriced
            FROM requests WHERE org_id=? AND upstream_account_id=? AND created_at>=? AND created_at<=?
            AND COALESCE(pricing_status,'') NOT IN ('ignored','not_billable') GROUP BY model""",
            (org['id'],account.get('id'),start,observed)).fetchall()
        row.update(requests=sum(u['n'] for u in usage),
                   tokens=sum(u['tokens'] for u in usage),
                   raw_usd=sum(u['usd'] for u in usage),
                   usd=sum(u['usd'] * billing.multiplier_for(rates,u['model'])[0] for u in usage))
        unpriced = sum(u['unpriced'] or 0 for u in usage)
        if not 0 <= now-observed <= 15*60000 or end <= now:
            row['reasons'].append('快照过期，需刷新')
        if pending or not watermark or watermark[0] is None or watermark[0] < observed:
            row['reasons'].append('流水尚未采集到快照时刻，请 /sync 后重查')
        row['unpriced'] = unpriced
        if pct is None or not 5 <= pct < 100:
            row['reasons'].append('使用率不足 5%、缺失或已封顶，不能可靠反推')
        if row['usd'] <= 0:
            row['reasons'].append('已采集用量不足')
        if not row['reasons']:
            row['estimate'] = row['usd']/(pct/100)
            row['remaining'] = row['estimate']-row['usd']
        result.append(row)
    return result


def _fetch_org(pool, login_id, refresh=False):
    with pool.session(login_id) as session:
        own = session.device_quota().get('account_org_id')
        if own is None:
            raise ValueError('无法确认自有组织')
        if refresh:
            session.post(f'/api/app/account/usage/refresh?org_id={int(own)}')
        org = next((o for o in session.get('/api/app/orgs/').get('items',[]) if o['id']==own), None)
    if not org:
        raise ValueError('未找到自有组织')
    return org


def fetch(pool, conn, login_id, refresh=False):
    org = _fetch_org(pool, login_id, refresh)
    account = org.get('current_account') or {}
    observed = timestamp(account.get('usage_updated_at'))
    now = store.now_ms()
    snapshot = account.get('usage_snapshot') or {}
    expired = observed is None or not 0 <= now-observed <= 15*60000
    for key in ('five_hour', 'seven_day'):
        end = timestamp((snapshot.get(key) or {}).get('resets_at'))
        expired = expired or end is None or end <= now
    if expired and not refresh:
        org = _fetch_org(pool, login_id, True)
    # Freeze the refreshed snapshot, then bring history up to its timestamp.
    # Re-fetching after sync could move the target ahead of the watermark forever.
    observed = timestamp((org.get('current_account') or {}).get('usage_updated_at'))
    for attempt in range(3):
        watermark = conn.execute('SELECT last_synced_at FROM watermarks WHERE account_id=?',
                                 (login_id,)).fetchone()
        pending = conn.execute('SELECT 1 FROM sync_progress WHERE account_id=?',
                               (login_id,)).fetchone()
        if not pending and watermark and watermark[0] is not None and (
                observed is None or watermark[0] >= observed):
            break
        syncer.sync_account(pool, conn, login_id)
    backfill_failed = False
    try:
        syncer.backfill_prices(pool, conn, login_id)
    except Exception:
        conn.rollback()
        backfill_failed = True
    windows = summarize(conn, login_id, org)
    for window in windows:
        window['backfill_failed'] = backfill_failed
    return org, windows
