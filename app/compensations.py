"""Apply upstream cumulative suspension compensation to paid rental periods."""
from datetime import datetime

from . import billing


def sync(session, conn, org_id):
    # Notifications are newest-first and suspension/restoration reports overlap.
    # Read the complete feed before changing dates; a failed page changes nothing.
    events = {}
    offset = 0
    while True:
        page = session.get(f'/api/app/notifications?limit=200&offset={offset}')
        for notification in page:
            if notification['kind'] not in ('subscription_suspended', 'subscription_compensated'):
                continue
            payload = notification['payload']
            if payload.get('org_id') != org_id or not payload.get('compensated_ms'):
                continue
            sid, since, total = (payload[k] for k in ('suspension_id', 'since', 'compensated_ms'))
            if any(type(v) is not int or v <= 0 for v in (sid, since, total)):
                raise ValueError('上游挂起补偿数据无效')
            previous = events.get(sid)
            if previous is None or total > previous[1]:
                events[sid] = (since, total)
        if len(page) < 200:
            break
        offset += len(page)
    try:
        conn.execute("UPDATE settings SET value=value WHERE key='subscription_ledger_v1'")
        for sid, (since, total) in events.items():
            conn.execute('INSERT INTO subscription_compensations VALUES (?,?,?,?) '
                         'ON CONFLICT(org_id,suspension_id) DO UPDATE SET '
                         'compensated_ms=MAX(compensated_ms,excluded.compensated_ms)',
                         (org_id, sid, since, total))
        for unit in conn.execute('SELECT id FROM billing_units WHERE org_id=?', (org_id,)).fetchall():
            apply(conn, unit['id'])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply(conn, unit_id, *, included_period=None):
    """Caller holds the SQLite write transaction; costs never change."""
    events = conn.execute('SELECT c.* FROM subscription_compensations c '
                          'JOIN billing_units u ON u.org_id=c.org_id WHERE u.id=? '
                          'ORDER BY c.since_ms,c.suspension_id', (unit_id,)).fetchall()
    for event in events:
        prior = conn.execute('SELECT * FROM period_compensations WHERE unit_id=? AND suspension_id=?',
                             (unit_id, event['suspension_id'])).fetchone()
        delta = event['compensated_ms'] - (prior['applied_ms'] if prior else 0)
        if delta <= 0:
            continue
        ps = sorted(billing.periods(conn, unit_id), key=lambda p: billing.period_bounds(p)[0])
        target = next((p for p in ps if p['id'] == prior['period_id']), None) if prior else next(
            (p for p in ps if billing.period_bounds(p)[0] <= event['since_ms'] < billing.period_bounds(p)[1]), None)
        if target is None:
            continue
        # auto/auto already includes all compensation observed before its creation.
        if target['id'] != included_period:
            start, _ = billing.period_bounds(target)
            for period in ps:
                lo, hi = billing.period_bounds(period)
                if lo < start:
                    continue
                new_start = lo if period['id'] == target['id'] else lo + delta
                conn.execute('UPDATE account_periods SET start_date=?,end_date=? WHERE id=?',
                             (_iso(new_start), _iso(hi + delta), period['id']))
        conn.execute('INSERT INTO period_compensations VALUES (?,?,?,?) '
                     'ON CONFLICT(unit_id,suspension_id) DO UPDATE SET applied_ms=excluded.applied_ms',
                     (unit_id, event['suspension_id'], target['id'], event['compensated_ms']))


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, billing.BILLING_TZ).isoformat()
