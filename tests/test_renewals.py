from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from test_core import env
from test_bot import bot, ADMIN, _msg
from test_subscription_ledger import item
from app import billing, ledger, store, syncer


def test_minute_boundary_counts_request_once_and_renews(env):
    rt, _ = env
    c = rt.conn
    start = billing.parse_boundary('2026-11-01T19:33')
    end = billing.parse_boundary('2026-11-02T19:33')
    billing.add_period(c, '1:seat1', start, end, 100)
    second = billing.renew_period(c, '1:seat1', 30, 200, message_key='chat:1')
    assert second['start_date'] == '2026-11-02T19:33:00+08:00'
    assert second['end_date'] == '2026-12-02T19:33:00+08:00'
    ledger.bind(c, 1, 100, 111)
    for rid, delta in [(1, -1), (2, 0), (3, 1)]:
        request = item(rid, 1, 101)
        request['created_at'] = billing.boundary_ms(end) + delta
        syncer._upsert_request(c, 'a1', request)
    c.commit()
    result = billing.compute(c, date(2026, 11, 1), date(2026, 12, 31))
    assert [p['requests'] for p in result['periods']] == [1, 2]
    assert result['users'] == {111: 300}
    assert not result['anomalies']['uncovered']
    # Replay after commit must not create a third period.
    assert billing.renew_period(c, '1:seat1', 30, 200, message_key='chat:1')['id'] == second['id']
    assert len(billing.periods(c, '1:seat1')) == 2
    with pytest.raises(ValueError, match='重叠'):
        billing.add_period(c, '1:seat1', end, billing.parse_boundary('2026-11-03T19:33'), 100)
    c.rollback()


def test_legacy_date_end_remains_inclusive(env):
    rt, _ = env
    billing.add_period(rt.conn, '1:seat1', date(2026, 10, 13), date(2026, 11, 12), 850)
    p = billing.renew_period(rt.conn, '1:seat1', 30, 850, message_key='legacy')
    assert p['start_date'].startswith('2026-11-13T00:00')
    assert p['end_date'].startswith('2026-12-13T00:00')


def test_parallel_message_replay_is_atomic(env):
    rt, _ = env
    billing.add_period(rt.conn, '1:seat1', date(2026, 11, 1), date(2026, 11, 30), 100)
    def renew(_):
        return billing.renew_period(rt.conn, '1:seat1', 30, 850, message_key='same-message')['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(renew, range(2)))
    assert ids[0] == ids[1]
    assert len(billing.periods(rt.conn, '1:seat1')) == 2


def test_owner_start_scoped_and_auto_keeps_upstream_precision(env):
    rt, _ = env
    c = rt.conn
    start, end = 1791891181074, 1794483181074
    orgs = {'items': [
        {'id': 1, 'role': 'owner', 'created_at': start-1000, 'seat_count': 1,
         'subscription_expires_at': end, 'current_account': {'id': 101}},
        {'id': 2, 'role': 'admin', 'created_at': start+1000, 'seat_count': 1,
         'subscription_expires_at': end+1000, 'current_account': {'id': 202}}]}
    ledger.discover(c, 'a1', orgs, {'account_org_id': 1})
    me = {'subscription': {'period_started_at': start, 'expires_at': end}}
    ledger.observe_own_start(c, 1, orgs, me)
    ledger.observe_own_start(c, 2, orgs, me)
    auto_start, auto_end = billing.automatic_boundaries(c, '1:seat1')
    assert billing.boundary_ms(auto_start) == start
    billing.add_period(c, '1:seat1', auto_start, auto_end, 850)
    p = billing.renew_period(c, '1:seat1', 30, 850, message_key='auto-renew')
    assert billing.period_bounds(p)[0] == end
    with pytest.raises(ValueError, match='未能确认'):
        billing.automatic_boundaries(c, '2:seat1')


def test_renew_validation_and_admin_guard(bot):
    ledger.ensure_unit(bot.conn, 1, 101)
    billing.add_period(bot.conn, '1:seat1', date(2026, 11, 1), date(2026, 11, 30), 100)
    chat = _msg()['chat']
    bot.command(chat, _msg(), ADMIN, '/renew 1:seat1 30 850', None)
    assert '续费租期' in bot.tg.sent[-1]
    bot.command(_msg(uid=555)['chat'], _msg(uid=555), 555, '/renew 1:seat1 30 850', None)
    assert '仅管理员' in bot.tg.sent[-1]
    for days, cost in [(0, 100), (-1, 100), (30, float('nan')), (30, -1)]:
        with pytest.raises(ValueError):
            billing.renew_period(bot.conn, '1:seat1', days, cost, message_key='invalid')
    assert len(billing.periods(bot.conn, '1:seat1')) == 2


def test_compensation_extension_moves_next_renewal_without_new_cost(env):
    rt, _ = env
    c = rt.conn
    pid = billing.add_period(c, '1:seat1', billing.parse_boundary('2026-11-01T19:33'),
                             billing.parse_boundary('2026-11-10T19:33'), 850)
    billing.extend_period(c, pid, billing.parse_boundary('2026-11-12T19:33'))
    p = billing.renew_period(c, '1:seat1', 30, 900, message_key='after-extension')
    assert p['start_date'].startswith('2026-11-12T19:33')
    assert billing.periods(c, '1:seat1')[0]['cost_cny'] == 850
    with pytest.raises(ValueError, match='重叠'):
        billing.extend_period(c, pid, billing.parse_boundary('2026-11-13T19:33'))
