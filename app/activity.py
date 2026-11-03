"""Device activity is the latest upstream timestamp or collected request timestamp."""


def latest(conn, device, upstream=None):
    org = device['org_id']
    if org is not None:
        recent = conn.execute('SELECT MAX(created_at) FROM requests WHERE org_id=? AND rc_device_id=?',
                              (org, device['rc_device_id'])).fetchone()[0]
        observed = conn.execute('SELECT MAX(last_used_at) FROM devices WHERE org_id=? AND rc_device_id=?',
                                (org, device['rc_device_id'])).fetchone()[0]
    else:
        recent = conn.execute('SELECT MAX(created_at) FROM requests WHERE account_id=? AND rc_device_id=? AND org_id IS NULL',
                              (device['account_id'], device['rc_device_id'])).fetchone()[0]
        observed = device['last_used_at']
    values = [v for v in (observed, recent, upstream) if isinstance(v, (int, float)) and v > 0]
    return max(values) if values else None


def last_request(conn, device):
    """Actual request activity, independent of upstream process heartbeats."""
    if device['org_id'] is not None:
        return conn.execute('SELECT MAX(created_at) FROM requests WHERE org_id=? AND rc_device_id=?',
                            (device['org_id'], device['rc_device_id'])).fetchone()[0]
    return conn.execute('SELECT MAX(created_at) FROM requests WHERE account_id=? AND rc_device_id=? AND org_id IS NULL',
                        (device['account_id'], device['rc_device_id'])).fetchone()[0]
