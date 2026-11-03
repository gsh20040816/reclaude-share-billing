from test_core import env
from app import quota,store,syncer


def test_window_scope_and_estimate(env):
 rt,_=env;c=rt.conn;now=store.now_ms();start=now-3600000
 org={'id':1,'current_account':{'id':101,'usage_updated_at':now,'usage_snapshot':{
  'five_hour':{'utilization':25,'resets_at':start+5*3600000}}}}
 c.execute('INSERT INTO watermarks(account_id,last_synced_at) VALUES (?,?)',('a1',now))
 for rid,oid,aid,ts in [(1,1,101,now-1),(2,1,101,start-1),(3,2,101,now-1),(4,1,202,now-1),(5,1,101,now+1)]:
  syncer._upsert_request(c,'a1',dict(id=rid,org_id=oid,account_id=aid,device_id=100,created_at=ts,cost_total_usd=10,pricing_status='priced',total_tokens=100))
 w=quota.summarize(c,'a1',org,now)[0]
 assert w['requests']==1 and w['usd']==10 and w['estimate']==40 and w['remaining']==30
 c.execute('UPDATE watermarks SET last_synced_at=?',(now-1,))
 assert quota.summarize(c,'a1',org,now)[0]['estimate'] is None
 c.execute('UPDATE watermarks SET last_synced_at=?',(now,))
 org['current_account']['usage_snapshot']['five_hour']['utilization']=100
 assert quota.summarize(c,'a1',org,now)[0]['estimate'] is None
 org['current_account']['usage_snapshot']['five_hour']['utilization']=25
 assert quota.summarize(c,'a1',org,now+16*60000)[0]['estimate'] is None
