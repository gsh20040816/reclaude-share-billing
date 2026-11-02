from datetime import date

import pytest

from test_core import env
from test_bot import bot, ADMIN, _msg
from test_subscription_ledger import item
from app import billing, ledger, store, syncer


def org(account, expires=2000, seats=1):
    return {'items': [{'id': 1, 'seat_count': seats, 'subscription_expires_at': expires,
                      'current_account': {'id': account}}]}


def test_replacement_and_extension_keep_identity_and_cost(env):
    rt, _ = env
    c = rt.conn
    ledger.discover(c, 'a1', org(101), {})
    billing.add_period(c, '1:seat1', date(2026, 11, 1), date(2026, 11, 30), 850)
    syncer._upsert_request(c, 'a1', item(1, 1, 101))
    ledger.discover(c, 'a1', org(999, 3000), {})
    syncer._upsert_request(c, 'a1', item(2, 1, 999))
    assert {r[0] for r in c.execute('SELECT billing_unit_id FROM requests')} == {'1:seat1'}
    assert c.execute('SELECT COUNT(*) FROM billing_units').fetchone()[0] == 1
    assert c.execute('SELECT COUNT(*) FROM account_periods').fetchone()[0] == 1
    p = billing.periods(c, '1:seat1')[0]
    assert p['end_date'] == '2026-11-30' and p['cost_cny'] == 850
    assert c.execute('SELECT expires_at FROM org_subscriptions').fetchone()[0] == 3000
    ledger.discover(c, 'a1', org(999, 3000), {})
    assert c.execute('SELECT COUNT(*) FROM subscription_observations').fetchone()[0] == 2
    # Shortening is also recorded, never hidden by max(expiry).
    ledger.discover(c, 'a1', org(999, 2500), {})
    assert c.execute('SELECT expires_at FROM org_subscriptions').fetchone()[0] == 2500


def test_unknown_history_and_multiseat_replacement_need_mapping(env):
    rt, _ = env
    c = rt.conn
    ledger.discover(c, 'a1', org(101), {})
    syncer._upsert_request(c, 'a1', item(1, 1, 55))
    assert c.execute('SELECT billing_unit_id FROM requests').fetchone()[0] is None
    ledger.link_account(c, '1:seat1', 55)
    assert c.execute('SELECT billing_unit_id FROM requests').fetchone()[0] == '1:seat1'
    ledger.discover(c, 'a1', org(777, seats=2), {})
    assert ledger.ensure_unit(c, 1, 777) is None
    # Returning to one seat cannot erase a known ambiguous history.
    ledger.discover(c, 'a1', org(888), {})
    assert ledger.ensure_unit(c, 1, 888) is None
    assert c.execute('SELECT COUNT(*) FROM billing_units').fetchone()[0] == 1


def test_migration_preserves_costs_and_references(env):
    rt, _ = env
    c = rt.conn
    c.execute("DELETE FROM settings WHERE key='stable_seats_v1'")
    c.execute('DELETE FROM upstream_assignments')
    c.execute("UPDATE billing_units SET id='1:101'")
    c.execute("INSERT INTO account_periods(account_id,billing_unit_id,start_date,end_date,cost_cny,created_at) VALUES ('1:101','1:101','2026-11-01','2026-11-30',850,'now')")
    c.execute("INSERT INTO billing_unit_access VALUES ('a1','1:101')")
    c.commit()
    store.init(c)
    store.init(c)
    assert billing.periods(c, '1:seat1')[0]['cost_cny'] == 850
    assert c.execute('SELECT unit_id FROM billing_unit_access').fetchone()[0] == '1:seat1'
    assert ledger.ensure_unit(c, 1, 101) == '1:seat1'


def test_link_command_admin_only(bot):
    ledger.ensure_unit(bot.conn, 1, 101)
    chat = _msg()['chat']
    bot.command(chat, _msg(), ADMIN, '/unit_link 1:seat1 999', None)
    assert ledger.ensure_unit(bot.conn, 1, 999) == '1:seat1'
    bot.command(_msg(uid=555)['chat'], _msg(uid=555), 555, '/unit_link 1:seat1 888', None)
    assert '仅管理员' in bot.tg.sent[-1]
