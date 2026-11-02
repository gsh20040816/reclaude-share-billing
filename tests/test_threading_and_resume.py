"""Transaction isolation, shared account locks, and durable pagination regressions."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from test_core import env, FakeClient
from app import enroll, store, syncer
from app.runtime import Runtime


@pytest.mark.parametrize('worker_action', ['commit', 'rollback'])
def test_other_thread_cannot_finish_main_transaction(env, worker_action):
    rt, _ = env
    ready, act, done = threading.Event(), threading.Event(), threading.Event()

    def worker():
        try:
            connection = rt.conn.current()
            ready.set()
            assert act.wait(5)
            getattr(rt.conn, worker_action)()
            return connection
        finally:
            done.set()
            rt.conn.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        assert ready.wait(5)
        rt.conn.execute("INSERT INTO settings VALUES ('uncommitted', 'value')")
        act.set()
        assert done.wait(5)
        assert future.result() is not rt.conn.current()
    assert rt.conn.in_transaction
    if worker_action == 'commit':
        rt.conn.rollback()
        assert rt.conn.execute("SELECT value FROM settings WHERE key='uncommitted'").fetchone() is None
    else:
        rt.conn.commit()
        assert rt.conn.execute("SELECT value FROM settings WHERE key='uncommitted'").fetchone()[0] == 'value'


def test_parallel_writers_and_sync_use_independent_connections(env):
    rt, state = env
    state.add_item(1, 100, 1)
    start = threading.Barrier(4)

    def writer(number):
        try:
            rt.conn.current()
            start.wait(timeout=5)
            for i in range(20):
                if number == 0:
                    syncer.sync_account(rt.pool, rt.conn, 'a1')
                else:
                    rt.conn.execute('INSERT INTO settings VALUES (?,?)', (f'{number}:{i}', 'value'))
                    if i % 2:
                        rt.conn.commit()
                    else:
                        rt.conn.rollback()
        finally:
            rt.conn.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(writer, range(4)))
    assert rt.conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE '%:%'").fetchone()[0] == 30
    assert rt.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1


def test_resume_survives_runtime_restart_and_keeps_original_window(env):
    rt, state = env
    for rid in range(3):
        state.add_item(rid + 1, 100, 1)
    calls = []

    def history(client, **kw):
        calls.append(kw)
        idx = int(kw.get('cursor') or 0)
        return {'items': [state.items[idx]], 'next_cursor': str(idx + 1) if idx < 2 else None}

    with patch.object(FakeClient, 'history', history):
        first = syncer.sync_account(rt.pool, rt.conn, 'a1', max_pages=1)
        assert first['truncated']
        resumed = Runtime(db_path=rt.conn.path, config=rt.cfg,
                          client_factory=lambda row: FakeClient(row['id'], state))
        try:
            second = syncer.sync_account(resumed.pool, resumed.conn, 'a1', max_pages=2)
            assert not second['truncated']
            assert [c['cursor'] for c in calls] == [None, '1', '2']
            assert all(c['since'] == first['since'] and c['until'] == first['until'] for c in calls)
            assert resumed.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 3
            assert resumed.conn.execute('SELECT COUNT(*) FROM sync_progress').fetchone()[0] == 0
            assert resumed.conn.execute('SELECT last_synced_at FROM watermarks').fetchone()[0] == first['until']
        finally:
            resumed.close()


def test_page_failure_preserves_last_successful_cursor(env):
    rt, state = env
    state.add_item(1, 100, 1)
    def history(client, **kw):
        if kw['cursor']:
            raise RuntimeError('network failure')
        return {'items': state.items, 'next_cursor': 'next'}
    with patch.object(FakeClient, 'history', history), pytest.raises(RuntimeError):
        syncer.sync_account(rt.pool, rt.conn, 'a1')
    assert rt.conn.execute('SELECT cursor FROM sync_progress').fetchone()[0] == 'next'
    assert rt.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1
    assert rt.conn.execute('SELECT COUNT(*) FROM watermarks').fetchone()[0] == 0


def test_concurrent_syncs_share_account_lock_and_resume_in_order(env):
    rt, state = env
    for rid in range(2):
        state.add_item(rid + 1, 100, 1)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    cursors = []

    def history(client, **kw):
        cursors.append(kw['cursor'])
        idx = int(kw.get('cursor') or 0)
        if idx == 0:
            entered.set()
            assert release.wait(5)
        return {'items': [state.items[idx]], 'next_cursor': '1' if idx == 0 else None}

    def worker(second=False):
        try:
            if second:
                second_started.set()
            return syncer.sync_account(rt.pool, rt.conn, 'a1', max_pages=1)
        finally:
            rt.conn.close()

    with patch.object(FakeClient, 'history', history), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker)
        assert entered.wait(5)
        second = executor.submit(worker, True)
        assert second_started.wait(5)
        release.set()
        assert first.result()['truncated']
        assert not second.result()['truncated']
    assert cursors == [None, '1']


def test_expired_or_legacy_pending_never_claims_later_device(env):
    rt, state = env
    enroll.enroll(rt.pool, rt.conn, state='expired', tg_user_id=111,
                  device_name='alice', dry_run=False, max_wait=0)
    row = rt.conn.execute("SELECT note FROM login_states WHERE state='expired'").fetchone()
    note = json.loads(row[0])
    note['claim_until_ms'] = store.now_ms() - 1
    rt.conn.execute("UPDATE login_states SET note=? WHERE state='expired'", (json.dumps(note),))
    rt.conn.commit()
    state.devices = [{'id': 777, 'name': note['device_name']}]
    syncer.sync_account(rt.pool, rt.conn, 'a1')
    assert rt.conn.execute('SELECT user_id FROM devices WHERE rc_device_id=777').fetchone()[0] is None
    assert rt.conn.execute("SELECT status FROM login_states WHERE state='expired'").fetchone()[0] == 'needs_binding'
    enroll.bind_device(rt.conn, 'a1', 777, 111)
    assert rt.conn.execute("SELECT status FROM login_states WHERE state='expired'").fetchone()[0] == 'approved'


def test_live_idle_device_can_still_be_evicted(env):
    from app import evictor
    rt, state = env
    old = store.now_ms() - 3_600_000
    rt.conn.execute("INSERT INTO devices(account_id,rc_device_id,last_used_at,state) VALUES ('a1',100,?,'active')", (old,))
    rt.conn.commit()
    state.devices = [{'id': 100, 'last_used_at': old}]
    assert evictor.evict(rt.pool, rt.conn, 'a1', 100, dry_run=False)['revoked']


def test_failed_live_activity_check_prevents_revoke(env):
    from app import evictor
    rt, state = env
    rt.conn.execute("INSERT INTO devices(account_id,rc_device_id,last_used_at,state) VALUES ('a1',100,?,'active')", (store.now_ms()-3_600_000,))
    rt.conn.commit()
    with patch.object(FakeClient, 'get', side_effect=RuntimeError('offline')):
        with pytest.raises(evictor.EvictionError):
            evictor.evict(rt.pool, rt.conn, 'a1', 100, dry_run=False)
    assert not any(path.endswith('/revoke') for path, _ in state.posts)
    assert rt.conn.execute('SELECT state FROM devices WHERE rc_device_id=100').fetchone()[0] == 'active'
