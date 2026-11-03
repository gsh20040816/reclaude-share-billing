import pytest
from test_core import env
from app import activity, evictor, store, syncer


def test_latest_across_collectors_scoped_to_org(env):
    rt,_=env
    c=rt.conn
    syncer._upsert_devices(c,'a1',[{'id':10,'org_id':1,'last_used_at':100}])
    for rid,org,ts in [(1,1,200),(2,2,900)]:
        syncer._upsert_request(c,'other',{'id':rid,'org_id':org,'account_id':101,'device_id':10,'created_at':ts})
    d=c.execute('SELECT * FROM devices WHERE account_id=? AND rc_device_id=?',('a1',10)).fetchone()
    assert activity.latest(c,d)==200
    assert activity.latest(c,d,300)==300
    syncer._upsert_devices(c,'a1',[{'id':10,'org_id':1,'last_used_at':None}])
    assert activity.latest(c,d)==200


def test_recent_request_blocks_eviction_even_if_other_collector(env):
    rt,state=env
    now=store.now_ms()
    syncer._upsert_devices(rt.conn,'a1',[{'id':10,'org_id':1,'last_used_at':now-3600000}])
    syncer._upsert_request(rt.conn,'other',{'id':1,'org_id':1,'account_id':101,'device_id':10,'created_at':now})
    rt.conn.commit()
    assert evictor.candidates(rt.conn,'a1')==[]
    with pytest.raises(evictor.EvictionError):
        evictor.evict(rt.pool,rt.conn,'a1',10,dry_run=False)
    assert not state.posts


def test_eviction_priority_and_hard_request_boundary(env, monkeypatch):
    rt,_=env;c=rt.conn;now=store.now_ms()
    monkeypatch.setattr(store,'now_ms',lambda:now)
    # Old upstream activity first; fresh heartbeat falls back to real requests.
    for did,upstream,request in [(1,now-4*3600000,now-2*3600000),
                                 (2,now,now-8*3600000),
                                 (3,now-9*3600000,now-90*60000)]:
        syncer._upsert_devices(c,'a1',[{'id':did,'org_id':1,'last_used_at':upstream}])
        syncer._upsert_request(c,'other',{'id':did,'org_id':1,'account_id':101,
                                        'device_id':did,'created_at':request})
    c.execute("UPDATE devices SET state='active'")
    c.commit()
    assert [r['rc_device_id'] for r in evictor.candidates(c,'a1',min_idle=0)] == [1,2]
    assert evictor.choose_victim(c,'a1',exclude={1})['rc_device_id']==2
    with pytest.raises(evictor.EvictionError,match='90'):
        evictor.evict(rt.pool,c,'a1',3,reason='automatic',force=True,dry_run=False)


def test_background_heartbeat_does_not_block_idle_request_eviction(env):
    rt,state=env;c=rt.conn;now=store.now_ms()
    state.devices=[{'id':10,'org_id':1,'last_used_at':now}]
    syncer._upsert_devices(c,'a1',state.devices)
    state.add_item(1,10,1,created_at=now-2*3600000)
    syncer._upsert_request(c,'a1',state.items[0]);c.commit()
    assert evictor.evict(rt.pool,c,'a1',10,reason='automatic',dry_run=False)['revoked']


def test_new_request_discovered_during_flush_blocks_revoke(env):
    rt,state=env;c=rt.conn;now=store.now_ms()
    state.devices=[{'id':10,'org_id':1,'last_used_at':now-3*3600000}]
    syncer._upsert_devices(c,'a1',state.devices)
    state.add_item(1,10,1,created_at=now-60*60000)
    with pytest.raises(evictor.EvictionError,match='90'):
        evictor.evict(rt.pool,c,'a1',10,reason='automatic',dry_run=False)
    assert not any(path.endswith('/revoke') for path,_ in state.posts)
    assert evictor.evict(rt.pool,c,'a1',10,reason='manual',force=True,dry_run=False)['revoked']
