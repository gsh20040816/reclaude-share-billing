from datetime import date

import pytest

from test_core import env
from app import billing, compensations, ledger

DAY = 86400000


def ms(value):
    return billing.boundary_ms(billing.parse_boundary(value))


def add(c, start, end, cost=850, **kwargs):
    return billing.add_period(c, '1:seat1', billing.parse_boundary(start),
                              billing.parse_boundary(end), cost, **kwargs)


def event(sid, since, days, kind='subscription_suspended', org=1):
    return dict(kind=kind, payload=dict(org_id=org, suspension_id=sid,
                                       since=ms(since), compensated_ms=days * DAY))


class Feed:
    def __init__(self, events):
        self.events = events

    def get(self, path):
        offset = int(path.split('offset=')[1])
        return self.events[offset:offset + 200]


def test_cumulative_restoration_replay_and_later_periods(env):
    rt, _ = env
    c = rt.conn
    first = add(c, '2026-10-13T19:33:01.074', '2026-11-12T19:33:01.074')
    second = add(c, '2026-11-12T19:33:01.074', '2026-12-12T19:33:01.074', 900)
    feed = Feed([event(1, '2026-11-11T22:00', 1)])
    compensations.sync(feed, c, 1)
    feed.events = [event(2, '2026-11-13T22:00', 2, 'subscription_compensated'),
                   event(1, '2026-11-11T22:00', 2, 'subscription_compensated'),
                   event(1, '2026-11-11T22:00', 2)]
    compensations.sync(feed, c, 1)
    compensations.sync(feed, c, 1)
    ps = billing.periods(c, '1:seat1')
    assert [p['id'] for p in ps] == [first, second]
    assert [p['cost_cny'] for p in ps] == [850, 900]
    assert billing.period_bounds(ps[0]) == (ms('2026-10-13T19:33:01.074'), ms('2026-11-16T19:33:01.074'))
    assert billing.period_bounds(ps[1]) == (ms('2026-11-16T19:33:01.074'), ms('2026-12-16T19:33:01.074'))
    renewed = billing.renew_period(c, '1:seat1', 30, 900, message_key='new')
    assert billing.period_bounds(renewed)[0] == ms('2026-12-16T19:33:01.074')


def test_auto_first_period_does_not_double_count_existing_compensation(env):
    rt, _ = env
    c = rt.conn
    feed = Feed([event(1, '2026-11-11T22:00', 2)])
    compensations.sync(feed, c, 1)
    add(c, '2026-11-01T00:00', '2026-12-03T00:00', upstream_boundaries=True)
    compensations.sync(feed, c, 1)
    assert billing.period_bounds(billing.periods(c, '1:seat1')[0])[1] == ms('2026-12-03T00:00')
    feed.events = [event(1, '2026-11-11T22:00', 3)]
    compensations.sync(feed, c, 1)
    assert billing.period_bounds(billing.periods(c, '1:seat1')[0])[1] == ms('2026-12-04T00:00')


def test_pagination_organization_scope_and_failed_page(env):
    rt, _ = env
    c = rt.conn
    add(c, '2026-11-01', '2026-11-30')
    events = [dict(kind='order_paid')] * 200 + [event(1, '2026-11-11T22:00', 2),
                                                    event(2, '2026-11-11T22:00', 5, org=2)]
    class Broken(Feed):
        def get(self, path):
            if 'offset=200' in path:
                raise RuntimeError('network failed')
            return super().get(path)
    with pytest.raises(RuntimeError, match='network failed'):
        compensations.sync(Broken(events), c, 1)
    assert c.execute('select count(*) from subscription_compensations').fetchone()[0] == 0
    compensations.sync(Feed(events), c, 1)
    assert billing.period_bounds(billing.periods(c, '1:seat1')[0])[1] == ms('2026-12-03T00:00')


def test_transaction_rolls_back_dates_and_receipts(env, monkeypatch):
    rt, _ = env
    c = rt.conn
    add(c, '2026-11-01', '2026-11-30')
    original = compensations.apply
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('interrupted')
    monkeypatch.setattr(compensations, 'apply', fail)
    with pytest.raises(RuntimeError):
        compensations.sync(Feed([event(1, '2026-11-11T22:00', 2)]), c, 1)
    assert billing.period_bounds(billing.periods(c, '1:seat1')[0])[1] == ms('2026-12-01T00:00')
    assert c.execute('select count(*) from period_compensations').fetchone()[0] == 0


def test_live_auto_renew_reconciles_compensation_before_boundary(env, monkeypatch):
    from test_core import FakeClient
    rt, _ = env
    c = rt.conn
    add(c, '2026-11-01T00:00', '2026-12-01T00:00')
    expiry = ms('2026-12-03T00:00')
    original = FakeClient.get
    def get(client, path):
        if path == '/api/app/orgs/':
            return {'items': [{'id': 1, 'role': 'owner', 'seat_count': 1,
                               'subscription_expires_at': expiry, 'current_account': {'id': 101}}]}
        if path.startswith('/api/app/notifications?'):
            return [event(1, '2026-11-11T22:00', 2)]
        return original(client, path)
    monkeypatch.setattr(FakeClient, 'get', get)
    end = ledger.renewal_expiry(rt.pool, c, '1:seat1')
    with pytest.raises(ValueError, match='无新增续期'):
        billing.renew_period(c, '1:seat1', None, 850, message_key='comp-only', end_ms=end)
    expiry += 30 * DAY
    end = ledger.renewal_expiry(rt.pool, c, '1:seat1')
    renewed = billing.renew_period(c, '1:seat1', None, 900, message_key='paid', end_ms=end)
    assert billing.period_bounds(renewed) == (ms('2026-12-03T00:00'), ms('2027-01-02T00:00'))
