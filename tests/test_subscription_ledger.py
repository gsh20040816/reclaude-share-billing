from datetime import date, timedelta

import pytest

from test_core import env
from app import billing, enroll, ledger, store, syncer
from test_bot import bot, ADMIN, _msg


def item(rid, org, account, device=100, cost=10):
    return dict(id=rid, org_id=org, account_id=account, device_id=device,
                created_at=store.now_ms(), model='model', cost_total_usd=cost,
                pricing_status='priced')


def test_same_organization_different_subscriptions_bill_separately(env):
    rt, _ = env
    conn = rt.conn
    for rid, org, account, device, user in [(1, 10, 101, 100, 111), (2, 10, 202, 200, 222), (3, 20, 101, 300, 333)]:
        ledger.bind(conn, org, device, user)
        syncer._upsert_request(conn, 'a1', item(rid, org, account, device))
    conn.commit()
    day = date.today()
    for unit, cost in [('10:101', 100), ('10:202', 200), ('20:101', 300)]:
        billing.add_period(conn, unit, day, day, cost)
    bill = billing.compute(conn, day, day)
    assert bill['users'] == {111: 100, 222: 200, 333: 300}
    assert {l['billing_unit_id'] for l in bill['periods']} == {'10:101','10:202','20:101'}
    billing.create_invoices(conn, bill)
    assert {r[0] for r in conn.execute('SELECT billing_unit_id FROM invoice_lines')} == {'10:101','10:202','20:101'}


def test_shared_org_request_deduplicates_across_login_sources(env):
    rt, _ = env
    request = item(1, 10, 101)
    assert syncer._upsert_request(rt.conn, 'a1', request)
    assert not syncer._upsert_request(rt.conn, 'second-login', request)
    rt.conn.commit()
    row = rt.conn.execute('SELECT * FROM requests').fetchone()
    assert row['billing_unit_id'] == '10:101'
    assert row['upstream_account_id'] == 101
    assert row['account_id'] == 'a1'  # acquisition source, not the billing unit
    assert rt.conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 1


def test_legacy_replay_enriches_existing_request_without_losing_user(env):
    rt, _ = env
    old = item(1, 10, 101)
    del old['account_id']
    syncer._upsert_request(rt.conn, 'a1', old)
    rt.conn.execute('UPDATE requests SET user_id=111 WHERE id=1')
    assert not syncer._upsert_request(rt.conn, 'second', item(1, 10, 101))
    row = rt.conn.execute('SELECT billing_unit_id,user_id FROM requests').fetchone()
    assert tuple(row) == ('10:101',111)


def test_missing_upstream_id_never_uses_login_or_current_subscription(env):
    rt, _ = env
    ledger.ensure_unit(rt.conn, 10, 101)
    old = item(1, 10, 101)
    del old['account_id']
    syncer._upsert_request(rt.conn, 'a1', old)
    rt.conn.commit()
    day = date.today()
    billing.add_period(rt.conn, '10:101', day, day, 100)
    bill = billing.compute(rt.conn, day, day)
    assert bill['users'] == {}
    assert bill['anomalies']['unresolved_requests'] == 1
    with pytest.raises(ValueError, match='归属'):
        billing.create_invoices(rt.conn, bill)


def test_legacy_cost_requires_explicit_assignment(env):
    rt, _ = env
    rt.conn.execute("INSERT INTO account_periods(account_id,start_date,end_date,cost_cny,created_at) VALUES ('a1','2026-11-01','2026-11-30',2200,'now')")
    rt.conn.commit()
    bill = billing.compute(rt.conn, date(2026,11,1), date(2026,11,30))
    assert bill['users'] == {}
    assert len(bill['anomalies']['legacy_periods']) == 1
    with pytest.raises(ValueError, match='旧成本'):
        billing.create_invoices(rt.conn, bill)
    pid = bill['anomalies']['legacy_periods'][0]['id']
    billing.assign_legacy_period(rt.conn, pid, '1:101')
    assert billing.periods(rt.conn, '1:101')[0]['cost_cny'] == 2200
    assert rt.conn.execute('SELECT COUNT(*) FROM account_periods').fetchone()[0] == 1
    with pytest.raises(ValueError):
        billing.assign_legacy_period(rt.conn, pid, '1:101')


def test_overlap_is_per_subscription_not_per_login_or_org(env):
    rt, _ = env
    ledger.ensure_unit(rt.conn, 1, 202)
    rt.conn.commit()
    day = date.today()
    billing.add_period(rt.conn, '1:101', day, day, 100)
    billing.add_period(rt.conn, '1:202', day, day, 200)
    with pytest.raises(ValueError, match='重叠'):
        billing.add_period(rt.conn, '1:101', day, day+timedelta(days=1), 300)
    with pytest.raises(ValueError, match='登录标签'):
        billing.add_period(rt.conn, 'a1', day, day, 100)


def test_own_org_selected_by_quota_not_first_bound_subscription(env):
    rt, _ = env
    orgs = {'items': [
        {'id':424253, 'role':'admin', 'current_account':{'id':8858, 'status':'bound','subscription_type':'max_20x'}},
        {'id':25693, 'role':'owner', 'current_account':{'id':7989, 'status':'bound','subscription_type':'max_5x'}}]}
    assert ledger.discover(rt.conn, 'a1', orgs, {'account_org_id':25693,'account_max':4}) == 25693
    row = store.account_row(rt.conn, 'a1')
    assert row['own_org_id'] == 25693 and row['tier'] == 'max_5x'
    assert {r[0] for r in rt.conn.execute('SELECT unit_id FROM billing_unit_access')} == {'424253:8858','25693:7989'}


def test_device_binding_applies_to_shared_sources_but_not_other_org(env):
    rt, _ = env
    for aid, org in [('a1',10),('second',10),('other',20)]:
        rt.conn.execute("INSERT INTO devices(account_id,rc_device_id,org_id) VALUES (?,100,?)", (aid,org))
    syncer._upsert_request(rt.conn, 'second', item(1, 10, 101))
    syncer._upsert_request(rt.conn, 'other', item(2, 20, 202))
    rt.conn.commit()
    enroll.bind_device(rt.conn, 'a1', 100, 111)
    assert rt.conn.execute('SELECT user_id FROM requests WHERE id=1').fetchone()[0] == 111
    assert rt.conn.execute('SELECT user_id FROM requests WHERE id=2').fetchone()[0] is None
    assert rt.conn.execute("SELECT user_id FROM devices WHERE account_id='second'").fetchone()[0] == 111


def test_migration_preserves_old_costs_and_snapshots_and_resets_replay_once(env):
    rt, _ = env
    c = rt.conn
    c.execute("DELETE FROM settings WHERE key='subscription_ledger_v1'")
    c.execute("INSERT INTO account_periods(account_id,start_date,end_date,cost_cny,created_at) VALUES ('a1','2026-11-01','2026-11-30',100,'now')")
    c.execute("INSERT INTO invoices(user_id,period_start,period_end,total_cny,status,created_at) VALUES (111,'2026-11-01','2026-11-30',100,'final','now')")
    c.execute("INSERT INTO watermarks(account_id,last_synced_at) VALUES ('a1',123)")
    c.commit()
    store.init(c)
    assert c.execute('SELECT billing_unit_id FROM account_periods').fetchone()[0] is None
    assert c.execute('SELECT status FROM invoices').fetchone()[0] == 'legacy'
    assert not c.execute('SELECT * FROM watermarks').fetchall()
    assert store.account_row(c, 'a1')['own_org_id'] is None
    c.execute("INSERT INTO watermarks(account_id,last_synced_at) VALUES ('a1',456)")
    c.commit()
    store.init(c)
    assert c.execute('SELECT last_synced_at FROM watermarks').fetchone()[0] == 456


def test_bot_exposes_unit_costs_and_protects_admin_commands(bot):
    ledger.ensure_unit(bot.conn, 25693, 7989, 'own-org', 'max_5x')
    bot.conn.commit()
    chat = _msg()['chat']
    bot.command(chat, _msg(), ADMIN, '/units', None)
    assert '25693:7989' in bot.tg.sent[-1]
    bot.command(chat, _msg(), ADMIN, '/period add 25693:7989 2026-11-01 2026-11-30 100', None)
    assert billing.periods(bot.conn, '25693:7989')[0]['cost_cny'] == 100
    bot.command(chat, _msg(), ADMIN, '/period add a1 2026-12-01 2026-12-31 100', None)
    assert '登录标签' in bot.tg.sent[-1]
    bot.command(_msg(uid=555)['chat'], _msg(uid=555), 555, '/units', None)
    assert '仅管理员' in bot.tg.sent[-1]


def test_cli_cost_target_is_subscription_key():
    from app.cli import build_parser
    args = build_parser().parse_args(['period-add', '--unit', '25693:7989',
        '--start', '2026-11-01', '--end', '2026-11-30', '--cost', '100'])
    assert args.unit == '25693:7989'
