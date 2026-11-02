from unittest.mock import patch

from test_core import env, FakeClient
from app import store, syncer


def test_bootstrap_includes_old_history_then_switches_to_incremental(env):
    rt, state = env
    now = store.now_ms()
    state.add_item(1, 100, 2, created_at=now - 90 * 86_400_000)
    state.add_item(2, 100, 3, created_at=now - 60_000)
    calls = []
    def history(client, **kw):
        calls.append(kw)
        return {'items': [i for i in state.items if kw['since'] <= i['created_at'] <= kw['until']],
                'next_cursor': None}
    with patch.object(FakeClient, 'history', history):
        first = syncer.sync_account(rt.pool, rt.conn, 'a1')
        second = syncer.sync_account(rt.pool, rt.conn, 'a1')
    assert calls[0]['since'] == 0
    assert first['inserted'] == 2
    assert calls[1]['since'] == first['until'] - 10 * 60_000
    assert second['inserted'] == 0
    assert rt.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 2


def test_upgrade_resets_old_window_once_without_deleting_data(env):
    rt, state = env
    state.add_item(1, 100, 2)
    syncer._upsert_request(rt.conn, 'a1', state.items[0])
    rt.conn.execute("UPDATE requests SET user_id=111 WHERE id=1")
    rt.conn.execute("DELETE FROM settings WHERE key='full_history_bootstrap_v1'")
    rt.conn.execute("INSERT INTO watermarks(account_id,last_synced_at) VALUES ('a1',1000)")
    rt.conn.execute("INSERT INTO sync_progress VALUES ('a1',100,1000,'old-cursor')")
    rt.conn.commit()
    store.init(rt.conn)
    assert rt.conn.execute('SELECT COUNT(*) FROM watermarks').fetchone()[0] == 0
    assert rt.conn.execute('SELECT COUNT(*) FROM sync_progress').fetchone()[0] == 0
    assert rt.conn.execute('SELECT user_id FROM requests WHERE id=1').fetchone()[0] == 111
    rt.conn.execute("INSERT INTO watermarks(account_id,last_synced_at) VALUES ('a1',2000)")
    rt.conn.execute("INSERT INTO sync_progress VALUES ('a1',0,3000,'new-cursor')")
    rt.conn.commit()
    store.init(rt.conn)
    assert rt.conn.execute('SELECT last_synced_at FROM watermarks').fetchone()[0] == 2000
    assert rt.conn.execute('SELECT cursor FROM sync_progress').fetchone()[0] == 'new-cursor'
