from unittest.mock import patch
from test_core import env, FakeClient
from app import enroll, syncer, store, evictor


def test_single_unrelated_device_is_not_claimed(env):
    rt, state = env
    # Existing upstream device not yet in SQLite; the approved device is delayed.
    state.devices = [{'id': 100, 'name': 'someone-else'}]
    plan = enroll.enroll(rt.pool, rt.conn, state='alice', tg_user_id=111,
                         device_name='alice', dry_run=False, max_wait=0.001)
    assert rt.conn.execute('SELECT user_id FROM devices WHERE rc_device_id=100').fetchone()[0] is None


def test_pending_alice_does_not_claim_bobs_login(env):
    rt, state = env
    enroll.enroll(rt.pool, rt.conn, state='alice', tg_user_id=111,
                  device_name='alice', dry_run=False, max_wait=0)
    state.devices_next = [{'id': 777, 'name': '__approved__'}]
    with patch('app.enroll.time.sleep', return_value=None):
        enroll.enroll(rt.pool, rt.conn, state='bob', tg_user_id=222,
                      device_name='bob', dry_run=False, max_wait=0.001)
    assert rt.conn.execute('SELECT user_id FROM devices WHERE rc_device_id=777').fetchone()[0] == 222


def test_capped_sync_makes_progress_on_next_run(env):
    rt, state = env
    now = store.now_ms()
    rt.conn.execute("INSERT INTO watermarks(account_id,last_synced_at) VALUES ('a1',?)", (now-3_600_000,))
    rt.conn.commit()
    for rid in range(3):
        state.add_item(rid+1, 100, 1, created_at=now - 60_000*(rid+1))
    def history(client, **kw):
        idx = int(kw.get('cursor') or 0)
        return {'items': [state.items[idx]], 'next_cursor': str(idx+1) if idx<2 else None}
    with patch.object(FakeClient, 'history', history):
        for _ in range(3):
            syncer.sync_account(rt.pool, rt.conn, 'a1', max_pages=2)
    assert rt.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 3


def test_automatic_eviction_rechecks_recent_activity(env):
    rt, state = env
    now = store.now_ms()
    rt.conn.execute("INSERT INTO devices(account_id,rc_device_id,last_used_at,state) VALUES ('a1',100,?,'active')", (now-35*60_000,))
    rt.conn.commit()
    state.devices = [{'id':100, 'name':'active-user', 'last_used_at':now-60_000}]
    state.add_item(1, 100, 1, created_at=now-60_000)
    try:
        evictor.evict(rt.pool, rt.conn, 'a1', 100, dry_run=False, force=False)
    except evictor.EvictionError:
        pass
    assert not any(path.endswith('/revoke') for path, body in state.posts)
