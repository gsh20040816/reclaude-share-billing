"""Billing identity is (organization, upstream subscription), never a login."""
from . import store


def unit_key(org_id, upstream_account_id):
    try:
        org, account = int(org_id), int(upstream_account_id)
    except (TypeError, ValueError):
        raise ValueError("流水缺少有效的 org_id/account_id，不能确定计费账号") from None
    if org <= 0 or account <= 0:
        raise ValueError("组织和上游账号 ID 必须为正整数")
    return f"{org}:{account}"


def ensure_unit(conn, org_id, upstream_account_id, org_name=None, tier=None):
    key = unit_key(org_id, upstream_account_id)
    conn.execute(
        """INSERT INTO billing_units(id,org_id,upstream_account_id,org_name,tier)
           VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
           org_name=COALESCE(excluded.org_name,billing_units.org_name),
           tier=COALESCE(excluded.tier,billing_units.tier)""",
        (key, int(org_id), int(upstream_account_id), org_name, tier))
    return key


def discover(conn, login_id, orgs, quota):
    items = orgs.get('items') or []
    conn.execute('DELETE FROM billing_unit_access WHERE login_account_id=?', (login_id,))
    for org in items:
        account = org.get('current_account') or {}
        if account.get('id') is None:
            continue
        key = ensure_unit(conn, org['id'], account['id'], org.get('name'), account.get('subscription_type'))
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
