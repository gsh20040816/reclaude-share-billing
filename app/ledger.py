"""Stable local seats; physical upstream accounts are historical assignments."""
from . import store


def ensure_unit(conn, org_id, upstream_account_id, org_name=None, tier=None,
                *, current=False):
    org, account = int(org_id), int(upstream_account_id)
    if org <= 0 or account <= 0:
        raise ValueError("组织和上游账号 ID 必须为正整数")
    assignment = conn.execute('SELECT unit_id FROM upstream_assignments WHERE org_id=? '
                              'AND upstream_account_id=?', (org, account)).fetchone()
    key = assignment[0] if assignment else None
    snapshot = conn.execute('SELECT * FROM org_subscriptions WHERE org_id=?', (org,)).fetchone()
    units = conn.execute('SELECT id FROM billing_units WHERE org_id=? ORDER BY id', (org,)).fetchall()
    if key is None:
        # Only an observed replacement in a consistently single-seat organization
        # is unambiguous. Unknown historical accounts must remain unresolved.
        if current and snapshot and snapshot['seat_count'] == 1 and not snapshot['ever_multi'] and len(units) == 1:
            key = units[0][0]
        elif snapshot and (not current or units):
            return None
        else:
            key = f"{org}:seat{len(units) + 1}"
            conn.execute('INSERT INTO billing_units(id,org_id,upstream_account_id) VALUES (?,?,?)',
                         (key, org, account))
        conn.execute('INSERT INTO upstream_assignments VALUES (?,?,?,?)', (org, account, key, store.iso()))
        conn.execute('UPDATE requests SET billing_unit_id=? WHERE org_id=? AND upstream_account_id=? AND billing_unit_id IS NULL',
                     (key, org, account))
    conn.execute('UPDATE billing_units SET org_name=COALESCE(?,org_name),tier=COALESCE(?,tier) WHERE id=?',
                 (org_name, tier, key))
    return key


def observe(conn, org):
    org_id = int(org['id'])
    seats = org.get('seat_count')
    expires = org.get('subscription_expires_at')
    account = (org.get('current_account') or {}).get('id')
    latest = conn.execute('SELECT * FROM subscription_observations WHERE org_id=? ORDER BY id DESC LIMIT 1',
                          (org_id,)).fetchone()
    now = store.iso()
    if latest is None or (latest['expires_at'], latest['upstream_account_id'], latest['seat_count']) != (expires, account, seats):
        conn.execute('INSERT INTO subscription_observations(org_id,expires_at,upstream_account_id,seat_count,observed_at) '
                     'VALUES (?,?,?,?,?)', (org_id, expires, account, seats, now))
    conn.execute("""INSERT INTO org_subscriptions(org_id,seat_count,expires_at,observed_at,ever_multi) VALUES (?,?,?,?,?) ON CONFLICT(org_id) DO UPDATE SET
        started_at=CASE WHEN expires_at=excluded.expires_at THEN started_at ELSE NULL END,
        start_source=CASE WHEN expires_at=excluded.expires_at THEN start_source ELSE NULL END,
        seat_count=excluded.seat_count, expires_at=excluded.expires_at, observed_at=excluded.observed_at,
        ever_multi=MAX(org_subscriptions.ever_multi,excluded.ever_multi)""",
        (org_id, seats, expires, now, int(seats is not None and seats > 1)))


def link_account(conn, unit_id, upstream_account_id):
    unit = conn.execute('SELECT * FROM billing_units WHERE id=?', (unit_id,)).fetchone()
    if unit is None or int(upstream_account_id) <= 0:
        raise ValueError('计费单元不存在或账号 ID 无效')
    org, account = unit['org_id'], int(upstream_account_id)
    previous = conn.execute('SELECT unit_id FROM upstream_assignments WHERE org_id=? AND upstream_account_id=?',
                            (org, account)).fetchone()
    if previous and previous[0] != unit_id:
        raise ValueError('该账号已归属其他计费单元，不能覆盖历史归属')
    conn.execute('INSERT OR IGNORE INTO upstream_assignments VALUES (?,?,?,?)', (org, account, unit_id, store.iso()))
    conn.execute('UPDATE requests SET billing_unit_id=? WHERE org_id=? AND upstream_account_id=? AND billing_unit_id IS NULL',
                 (unit_id, org, account))


def discover(conn, login_id, orgs, quota):
    items = orgs.get('items') or []
    conn.execute('DELETE FROM billing_unit_access WHERE login_account_id=?', (login_id,))
    for org in items:
        observe(conn, org)
        account = org.get('current_account') or {}
        if account.get('id') is None:
            continue
        key = ensure_unit(conn, org['id'], account['id'], org.get('name'), account.get('subscription_type'), current=True)
        if key is None:
            continue
        conn.execute('INSERT OR IGNORE INTO billing_unit_access VALUES (?,?)', (login_id, key))
    # Quota explicitly identifies the account's own org; bound merely means a
    # subscription is attached and says nothing about login ownership.
    own = quota.get('account_org_id')
    if own is None:
        owners = [o['id'] for o in items if o.get('role') == 'owner']
        own = owners[0] if len(owners) == 1 else None
    selected = next((o for o in items if o['id'] == own), {})
    tier = (selected.get('current_account') or {}).get('subscription_type')
    conn.execute('UPDATE rc_accounts SET own_org_id=?,tier=?,capacity=? WHERE id=?',
                 (own, tier, quota.get('account_max', quota.get('max')), login_id))
    conn.commit()
    return own


def bind(conn, org_id, device_id, user_id):
    if org_id is None:
        raise ValueError('设备组织未知，请先同步流水后再绑定')
    if user_id is None:
        conn.execute('DELETE FROM device_bindings WHERE org_id=? AND rc_device_id=?', (org_id, device_id))
    else:
        conn.execute('INSERT INTO device_bindings VALUES (?,?,?) ON CONFLICT(org_id,rc_device_id) '
                     'DO UPDATE SET user_id=excluded.user_id', (org_id, device_id, user_id))
    conn.execute('UPDATE devices SET user_id=? WHERE org_id=? AND rc_device_id=?', (user_id, org_id, device_id))
    conn.execute('UPDATE requests SET user_id=? WHERE org_id=? AND rc_device_id=?', (user_id, org_id, device_id))


def observe_own_start(conn, own_org_id, orgs, me):
    """me's start belongs to the credential owner, even when another org is selected."""
    org = next((o for o in orgs.get('items', []) if o['id'] == own_org_id and o.get('role') == 'owner'), None)
    if not org:
        return
    sub = me.get('subscription') or {}
    start, end = sub.get('period_started_at'), sub.get('expires_at')
    valid = (isinstance(start, int) and isinstance(end, int) and 0 < start < end
             and end == org.get('subscription_expires_at')
             and start >= (org.get('created_at') or 0))
    conn.execute('UPDATE org_subscriptions SET started_at=?,start_source=? WHERE org_id=?',
                 (start if valid else None, 'owner subscription API' if valid else None, own_org_id))
    conn.commit()
