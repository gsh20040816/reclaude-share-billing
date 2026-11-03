"""Read-only account selection. Never evicts, approves, or moves devices."""
import math
from datetime import datetime
from . import store


class NoCapacity(Exception):
    pass


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def pressure(account, now):
    snapshot = account.get('usage_snapshot') or {}
    updated = number(account.get('usage_updated_at'))
    if updated is None or not 0 <= now - updated <= 15 * 60_000:
        return None
    windows = [snapshot.get('five_hour'), snapshot.get('seven_day')]
    windows += snapshot.get('limits') or []
    values = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        pct = number(window.get('utilization', window.get('percent')))
        if pct is None:
            continue
        reset = window.get('resets_at')
        if reset:
            try:
                if datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()*1000 <= now:
                    # A reset requires a new observation; don't invent zero usage.
                    return None
            except (ValueError, TypeError):
                return None
        values.append(pct / 100)
    return max(values) if values else None


def rank(candidates):
    """Minimize limiting-window pressure, with recent normalized spend as load signal.

    Unknown/stale usage is fallback only. A 20x plan's spend is divided by 20,
    a 5x plan's by 5; this is a relative load heuristic, not a dollar allowance.
    """
    valid = [c for c in candidates if c['available'] and (c['pressure'] is None or c['pressure'] < 1)]
    largest = max((c['normalized_usage'] for c in valid), default=0)
    return sorted(valid, key=lambda c: (
        c['pressure'] is None,
        .8 * (c['pressure'] or 0) + .2 * (c['normalized_usage']/largest if largest else 0),
        c['device_count'], c['org_id'], c['account_id']))


def collect(pool, conn, preferred=None):
    now = store.now_ms()
    candidates = []
    errors = []
    for login in store.enabled_accounts(conn):
        if preferred and login['id'] != preferred:
            continue
        if login['status'] not in ('ok', None):
            continue
        try:
            with pool.session(login['id']) as s:
                quota = s.device_quota()
                own_org = quota.get('account_org_id')
                if own_org is None:
                    continue  # unknown ownership must never fall back to a joined org
                orgs = [o for o in s.get('/api/app/orgs/').get('items', []) if o['id'] == own_org]
                # Refresh stale upstream snapshots; this is not a model request.
                for org in orgs:
                    if pressure(org.get('current_account') or {}, now) is None and (org.get('actions') or {}).get('usage_refresh'):
                        try:
                            s.post(f"/api/app/account/usage/refresh?org_id={int(org['id'])}")
                        except Exception:
                            pass
                orgs = [o for o in s.get('/api/app/orgs/').get('items', []) if o['id'] == own_org]
                quota, devices = s.device_quota(), s.devices()
                if quota.get('account_org_id') != own_org:
                    continue
            # Credential-level limit and own-account limit are different scopes.
            platform = next((p for p in quota.get('by_platform', []) if p.get('platform') == 'claude'), quota)
            used, cap = number(platform.get('used')), number(platform.get('max'))
            if used is None or cap is None:
                continue  # never infer free device slots from a missing response
            for org in orgs:
                account = org.get('current_account') or {}
                if account.get('status') != 'bound' or not account.get('id'):
                    continue
                if org.get('platform') not in (None, 'claude'):
                    continue
                if org.get('subscription_expires_at') and org['subscription_expires_at'] <= now:
                    continue
                available = used < cap
                au, ac = number(quota.get('account_used')), number(quota.get('account_max'))
                available = available and au is not None and ac is not None and au < ac
                tier = account.get('subscription_type')
                scale = store.get_float(conn, f'allocation_capacity:{tier}', {'max_5x':5, 'max_20x':20}.get(tier, 1))
                scale = scale if math.isfinite(scale) and scale > 0 else 1
                usage = conn.execute('SELECT COALESCE(SUM(cost_usd),0) FROM requests '
                                     'WHERE org_id=? AND upstream_account_id=? AND created_at>=?',
                                     (org['id'], account['id'], now-7*86400_000)).fetchone()[0]
                candidates.append(dict(account_id=login['id'], org_id=org['id'], upstream_account_id=account['id'],
                                       pressure=pressure(account, store.now_ms()), normalized_usage=usage/scale,
                                       usage_7d_usd=usage, capacity_scale=scale, available=available,
                                       device_count=sum(d.get('org_id') == org['id'] for d in devices)))
        except Exception as exc:
            errors.append(f"{login['id']}: {type(exc).__name__}")
    # Same upstream/org through several login credentials is a single candidate.
    result = []
    seen = set()
    for candidate in rank(candidates):
        key = candidate['org_id'], candidate['upstream_account_id']
        if key not in seen:
            result.append(candidate)
            seen.add(key)
    return result, errors


def choose(pool, conn, preferred=None):
    candidates, errors = collect(pool, conn, preferred)
    if not candidates:
        raise NoCapacity('没有满足设备名额与额度条件的账号；不会自动下线已有设备' +
                         ('（部分入口读取失败：' + ', '.join(errors) + '）' if errors else ''))
    return candidates[0]
