from datetime import date
from test_core import env
from test_bot import bot,ADMIN,_msg
from app import billing,ledger,syncer


def prepare(c):
 ledger.ensure_unit(c,2,202)
 for unit,start,end,cost in [('1:seat1','2026-10-13','2026-11-12',850),('2:seat1','2026-10-27','2026-11-26',1870)]:
  billing.add_period(c,unit,date.fromisoformat(start),date.fromisoformat(end),cost)
 for rid,org,account in [(1,1,101),(2,2,202)]:
  ledger.bind(c,org,100,111)
  syncer._upsert_request(c,'a1',dict(id=rid,org_id=org,account_id=account,device_id=100,created_at=billing.day_start_ms(date(2026,11,1)),cost_total_usd=10,pricing_status='priced'))
 c.commit()


def test_each_user_each_period_saved_independently_and_filter_idempotent(env):
 rt,_=env;c=rt.conn;prepare(c)
 billing.create_invoices(c,billing.compute(c,date(2026,11,1),date(2026,11,30)))
 rows=c.execute('SELECT * FROM invoices ORDER BY subscription_period_id').fetchall()
 assert len(rows)==2
 assert [r['total_cny'] for r in rows]==[850,1870]
 assert [r['period_start'] for r in rows]==['2026-10-13','2026-10-27']
 billing.create_invoices(c,billing.compute(c,date(2026,11,1),date(2026,11,1)))
 assert c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]==2
 # Refresh just the later period, preserving the other period's invoice.
 billing.create_invoices(c,billing.compute(c,date(2026,11,20),date(2026,11,20)))
 assert c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]==2
 assert c.execute('SELECT COUNT(*) FROM invoice_lines').fetchone()[0]==2


def test_report_sends_separate_period_bills(bot):
 ledger.ensure_unit(bot.conn,1,101);prepare(bot.conn)
 bot.command(_msg()['chat'],_msg(),ADMIN,'/report 2026-11-01 2026-11-30',None)
 reports=[s for s in bot.tg.sent if '订阅周期账单 #' in s]
 assert len(reports)==2
 assert '¥850.00' in reports[0] and '¥1,870.00' not in reports[0]
 assert '¥1,870.00' in reports[1] and '¥850.00' not in reports[1]
