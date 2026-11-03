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
 assert quota.summarize(c,'a1',org,now+31*60000)[0]['estimate'] is None


def test_weighted_estimate_includes_pending_without_blocking(env):
 rt,_=env;c=rt.conn;now=store.now_ms()
 c.execute('INSERT INTO watermarks(account_id,last_synced_at) VALUES (?,?)',('a1',now))
 c.executemany('INSERT INTO model_rates(pattern,multiplier,updated_at) VALUES (?,?,?)',
               [('claude-*',2,store.iso()),('claude-exact',3,store.iso())])
 c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('default_multiplier','4')")
 for rid,model,cost in [(1,'claude-exact',10),(2,'claude-other',10),(3,'other',10),(4,'other',None)]:
  syncer._upsert_request(c,'a1',dict(id=rid,org_id=1,account_id=101,device_id=100,created_at=now-1,
   model=model,cost_total_usd=cost,pricing_status='priced' if cost else 'pending',total_tokens=100))
 org={'id':1,'current_account':{'id':101,'usage_updated_at':now,'usage_snapshot':{
  'five_hour':{'utilization':25,'resets_at':now+3600000}}}}
 w=quota.summarize(c,'a1',org,now)[0]
 assert w['raw_usd']==30 and w['usd']==90
 assert w['estimate']==360 and w['remaining']==270
 assert w['unpriced']==1 and not w['reasons']
 assert w['requests']==4 and w['tokens']==400 and w['percent']==25


def test_expired_snapshot_refreshes_before_sync(env, monkeypatch):
 rt,_=env;c=rt.conn;now=store.now_ms();calls=[]
 def fetch_org(pool,login_id,refresh=False,conn=None):
  calls.append(('fetch',refresh))
  return {'id':1,'current_account':{'id':101,'usage_updated_at':now,
   'usage_snapshot':{key:{'utilization':25,'resets_at':now+3600000}
                     for key in ('five_hour','seven_day')}}}
 def sync(pool,conn,login_id):
  calls.append(('sync',login_id))
  conn.execute('INSERT OR REPLACE INTO watermarks(account_id,last_synced_at) VALUES (?,?)',(login_id,now))
  syncer._upsert_request(conn,login_id,dict(id=1,org_id=1,account_id=101,device_id=100,created_at=now-1,
   cost_total_usd=10,pricing_status='priced'))
 monkeypatch.setattr(quota,'_fetch_org',fetch_org)
 monkeypatch.setattr(syncer,'sync_account',sync)
 monkeypatch.setattr(syncer,'backfill_prices',lambda *a:calls.append(('backfill',)))
 _,windows=quota.fetch(rt.pool,c,'a1')
 assert calls==[('fetch',False),('sync','a1'),('backfill',)]
 assert windows[0]['estimate']==40
 calls.clear()
 quota.fetch(rt.pool,c,'a1',refresh=True)
 assert calls==[('fetch',True),('backfill',)]

 def fail_backfill(*args):
  raise RuntimeError('upstream unavailable')
 c.commit()
 monkeypatch.setattr(syncer,'backfill_prices',fail_backfill)
 _,windows=quota.fetch(rt.pool,c,'a1',refresh=True)
 assert windows[0]['estimate']==40 and windows[0]['backfill_failed']


def test_refresh_age_cooldown_and_failure(env, monkeypatch):
 from app import quota_refresh
 rt,_=env;c=rt.conn;now=store.now_ms();posts=[]
 monkeypatch.setattr(store,'now_ms',lambda:now)
 class Session:
  def post(self,path):
   posts.append(path)
   raise RuntimeError('HTTP 429')
 s=Session()
 org={'id':1,'current_account':{'usage_updated_at':now-20*60000,
      'usage_snapshot':{'five_hour':{'resets_at':None,'utilization':0}}}}
 assert not quota_refresh.refresh_usage(s,c,org)
 assert not posts
 org['current_account']['usage_updated_at']=now-30*60000
 assert not quota_refresh.refresh_usage(s,c,org)
 assert not posts
 now+=1
 assert not quota_refresh.refresh_usage(s,c,org)
 assert len(posts)==1
 assert not quota_refresh.refresh_usage(s,c,org,force=True)
 assert len(posts)==1
 now+=30*60000
 assert not quota_refresh.refresh_usage(s,c,org)
 assert len(posts)==2


def test_fetch_keeps_snapshot_when_refresh_limited(env, monkeypatch):
 from contextlib import contextmanager
 rt,_=env;c=rt.conn;now=store.now_ms()
 org={'id':1,'current_account':{'usage_updated_at':now-3600000}}
 class Session:
  def device_quota(self):return {'account_org_id':1}
  def get(self,path):return {'items':[org]}
  def post(self,path):raise RuntimeError('HTTP 429')
 class Pool:
  @contextmanager
  def session(self,login):yield Session()
 assert quota._fetch_org(Pool(),'a1',conn=c)==org
