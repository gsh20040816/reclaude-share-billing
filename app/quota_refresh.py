"""Shared per-organization cooldown for upstream quota refreshes."""
from datetime import datetime
from . import store

INTERVAL_MS = 30 * 60_000


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 0 else None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(dt.timestamp()*1000) if dt.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def refresh_usage(session, conn, org, force=False):
    now = store.now_ms()
    observed = timestamp((org.get('current_account') or {}).get('usage_updated_at'))
    key = f"quota_refresh_attempt:{org['id']}"
    last = store.get_int(conn, key, 0)
    if last and now-last < INTERVAL_MS:
        return False
    if not force and observed is not None and now-observed <= INTERVAL_MS:
        return False
    # Persist attempts as well as successes so a 429 cannot trigger a retry storm.
    store.set_setting(conn, key, now)
    try:
        session.post(f"/api/app/account/usage/refresh?org_id={int(org['id'])}")
    except Exception:
        return False
    return True
