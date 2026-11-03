from unittest.mock import patch
import pytest
from test_core import env, FakeClient
from app import allocation, enroll, store


def candidate(org, pct, spend=0, scale=5, available=True):
    return dict(org_id=org, account_id='a1', pressure=pct, normalized_usage=spend/scale,
                device_count=0, available=available)


def test_real_imbalance_and_exhaustion():
    assert allocation.rank([candidate(1,1,500),candidate(2,.59,1000,20)])[0]['org_id']==2
    assert allocation.rank([candidate(1,.96),candidate(2,.59)])[0]['org_id']==2


def test_recent_usage_and_plan_capacity_influence_equal_quota():
    assert allocation.rank([candidate(1,.5,500,5),candidate(2,.5,1000,20)])[0]['org_id']==2
    assert allocation.rank([candidate(1,.5,10),candidate(2,.5,100)])[0]['org_id']==1


def test_stale_is_not_zero_and_full_is_excluded():
    assert allocation.rank([candidate(1,None),candidate(2,.8),candidate(3,0,available=False)])[0]['org_id']==2
    now=store.now_ms()
    a={'usage_updated_at':now,'usage_snapshot':{'five_hour':{'utilization':8},'seven_day':{'utilization':96},'limits':[{'percent':100}]}}
    assert allocation.pressure(a,now)==1
    assert allocation.pressure(a,now+16*60_000) is None
    a['usage_snapshot']['five_hour']['resets_at']='2020-01-01T00:00:00Z'
    assert allocation.pressure(a,now) is None


def test_full_never_calls_revoke_or_approve(env):
    rt,state=env
    state.quota['used']=state.quota['max']
    with patch('app.evictor.evict',side_effect=AssertionError('must not revoke')):
        with pytest.raises(enroll.NoSlot):
            enroll.enroll(rt.pool,rt.conn,state='full',tg_user_id=111,device_name='test',dry_run=False)
    assert not any(p.endswith('/approve') or p.endswith('/revoke') for p,b in state.posts)


def test_selects_other_accessible_org_and_approves_that_org(env):
    rt,state=env
    original=FakeClient.get
    def get(client,path):
        if path=='/api/app/orgs/':
            return {'items':[{'id':oid,'platform':'claude','current_account':{'id':aid,'status':'bound','subscription_type':tier,
                'usage_updated_at':store.now_ms(),'usage_snapshot':{'five_hour':{'utilization':pct}}}}
                for oid,aid,tier,pct in [(1,101,'max_5x',96),(2,202,'max_20x',48)]]}
        return original(client,path)
    with patch.object(FakeClient,'get',get):
        result=enroll.enroll(rt.pool,rt.conn,state='test',tg_user_id=111,device_name='test',dry_run=False,max_wait=0)
    assert result['org_id']==2
    assert next(b for p,b in state.posts if p.endswith('/approve'))['org_id']==2
    assert not any(p.endswith('/revoke') for p,b in state.posts)
