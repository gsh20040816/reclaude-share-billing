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
