"""核心逻辑测试：幂等采集、归因、补价、分摊、闲置门槛、演练开通。

用 FakeClient 完全离线，不碰真实 ReClaude。
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, enroll, evictor, store, syncer          # noqa: E402
from app.runtime import Runtime                                   # noqa: E402


class FakeState:
    def __init__(self):
        self.items = []
        self.devices = []
        self.devices_next = None
        self.quota = {"max": 4, "used": 0, "account_max": 4, "account_used": 0}
        self.describe = {"hostname": "test-laptop", "os": "linux", "arch": "amd64",
                         "expires_at": int(time.time() * 1000) + 600_000,
                         "suggested_org_id": 1}
        self.posts = []

    def add_item(self, rid, device_id, cost, model="claude-opus-5-5", status="priced",
                 created_at=None):
        self.items.append({
            "id": rid, "device_id": device_id, "org_id": 1,
            "created_at": created_at or (store.now_ms() - 60_000),
            "model": model, "request_kind": "messages", "status": 200, "is_stream": True,
            "duration_ms": 1000, "input_tokens": 100, "output_tokens": 200,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
            "cache_read_input_tokens": 300, "total_tokens": 600,
            "cost_total_usd": f"{cost:.8f}" if status == "priced" else None,
            "pricing_status": status, "deduction_status": "none", "tracking_id": f"req_{rid}",
        })


class FakeClient:
    def __init__(self, account_id, state: FakeState):
        self.account_id = account_id
        self.state = state
        self.cookie = "rc_sid=fake-cookie"

    def get(self, path):
        if path.startswith("/api/app/devices/quota"):
            return self.state.quota
        if path.startswith("/api/app/devices"):
            return self.state.devices
        if path.startswith("/api/app/usage/stats"):
            return {"overview": {"total_usd": "0", "heatmap": []}}
        raise AssertionError("unexpected GET " + path)

    def post(self, path, body=None):
        self.state.posts.append((path, body))
        if path == "/api/cli/auth/describe":
            return self.state.describe
        if path == "/api/cli/auth/approve":
            if self.state.devices_next is not None:
                self.state.devices = self.state.devices_next
            return {"ok": True}
        if path.endswith("/revoke"):
            return {"ok": True}
        return {}

    def history(self, **kw):
        return {"items": list(self.state.items), "next_cursor": None}

    def devices(self):
        return self.state.devices

    def device_quota(self):
        return self.state.quota


@pytest.fixture()
def env(tmp_path):
    state = FakeState()
    rt = Runtime(db_path=str(tmp_path / "t.db"),
                 config={"bot_token": "", "admin_ids": [], "allowed_chat_ids": [],
                         "base_url": "https://example.invalid", "dry_run": True},
                 client_factory=lambda row: FakeClient(row["id"], state))
    rt.conn.execute("INSERT INTO rc_accounts(id, label, email, password_enc, status, own_org_id,"
                    " capacity, created_at) VALUES ('a1','a1','x@y.z','', 'ok',1,4,?)",
                    (store.iso(),))
    rt.conn.commit()
    yield rt, state
    rt.close()


def test_sync_idempotent_and_attribution(env):
    rt, state = env
    rt.conn.execute("INSERT INTO devices(account_id, rc_device_id, user_id, name, state, first_seen_at)"
                    " VALUES ('a1', 100, 111, 'u1', 'active', ?)", (store.iso(),))
    rt.conn.commit()
    for i in range(3):
        state.add_item(1000 + i, 100, 2.0)

    first = syncer.sync_account(rt.pool, rt.conn, "a1")
    second = syncer.sync_account(rt.pool, rt.conn, "a1")

    assert first["inserted"] == 3
    assert second["inserted"] == 0
    rows = rt.conn.execute("SELECT * FROM requests").fetchall()
    assert len(rows) == 3
    assert all(r["user_id"] == 111 for r in rows)
    wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id='a1'").fetchone()
    assert wm["last_synced_at"] > 0


def test_price_backfill(env):
    rt, state = env
    rt.conn.execute("INSERT INTO devices(account_id, rc_device_id, user_id, name, state, first_seen_at)"
                    " VALUES ('a1', 200, 222, 'u2', 'active', ?)", (store.iso(),))
    rt.conn.commit()
    state.add_item(2001, 200, 0, status="pending")
    syncer.sync_account(rt.pool, rt.conn, "a1")
    assert rt.conn.execute("SELECT cost_usd FROM requests WHERE id=2001").fetchone()[0] is None

    state.items = []
    state.add_item(2001, 200, 1.5, status="priced")
    out = syncer.backfill_prices(rt.pool, rt.conn, "a1")
    assert out["priced"] >= 1
    assert abs(rt.conn.execute("SELECT cost_usd FROM requests WHERE id=2001").fetchone()[0] - 1.5) < 1e-9


def test_billing_split_exact_and_multiplier(env):
    rt, _ = env
    today = date.today()
    billing.add_period(rt.conn, "a1", today - timedelta(days=1), today + timedelta(days=10), 1000.0)
    rt.conn.execute("INSERT INTO model_rates(pattern, multiplier, updated_at) VALUES ('claude-opus-5-5', 2.0, ?)",
                    (store.iso(),))
    ts = store.now_ms() - 1000
    for rid, uid, usd, model in ((1, 111, 10.0, "claude-haiku-5-5"),
                                 (2, 222, 10.0, "claude-opus-5-5")):
        rt.conn.execute(
            """INSERT INTO requests(id, account_id, rc_device_id, user_id, created_at, model,
                                    cost_usd, pricing_status, total_tokens, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rid, "a1", 100 + rid, uid, ts, model, usd, "priced", 1000, store.iso()))
    rt.conn.commit()

    bill = billing.compute(rt.conn, today - timedelta(days=1), today + timedelta(days=10))
    # user111: w=10, user222: w=20 → 1/3 与 2/3
    assert abs(bill["users"][111] - 333.33) < 0.02
    assert abs(bill["users"][222] - 666.67) < 0.02
    assert abs(sum(bill["users"].values()) - 1000.0) < 0.02
    assert bill["periods"][0]["status"] == "provisional"


def test_period_overlap_rejected(env):
    rt, _ = env
    today = date.today()
    billing.add_period(rt.conn, "a1", today, today + timedelta(days=30), 2200)
    with pytest.raises(ValueError):
        billing.add_period(rt.conn, "a1", today + timedelta(days=10), today + timedelta(days=40), 2200)


def test_eviction_respects_min_idle(env):
    rt, _ = env
    now = store.now_ms()
    for rid, last in ((301, now - 5 * 60_000), (302, now - 3 * 3_600_000)):
        rt.conn.execute(
            "INSERT INTO devices(account_id, rc_device_id, user_id, name, state, last_used_at, first_seen_at)"
            " VALUES ('a1', ?, ?, ?, 'active', ?, ?)",
            (rid, 500 + rid, f"d{rid}", last, store.iso()))
    rt.conn.commit()
    cands = evictor.candidates(rt.conn, "a1")
    assert [c["rc_device_id"] for c in cands] == [302]      # 5 分钟前用过的不能被抢
    assert evictor.choose_victim(rt.conn, "a1")["rc_device_id"] == 302


def test_enroll_dry_run_does_not_approve(env):
    rt, state = env
    plan = enroll.enroll(rt.pool, rt.conn, state="STATE123456", tg_user_id=999,
                         device_name="u999@999", dry_run=True)
    assert plan["account_id"] == "a1"
    assert plan["device_id"] is None
    assert all(p[0] != "/api/cli/auth/approve" for p in state.posts)


def test_enroll_approve_registers_device(env):
    rt, state = env
    state.devices_next = [{"id": 777, "name": "u999@999", "org_id": 1, "sk_prefix": "sk-rec-x",
                           "last_used_at": store.now_ms(), "created_at": store.now_ms(),
                           "machine_summary": {}, "client_platform": "linux"}]
    enroll.enroll(rt.pool, rt.conn, state="STATE654321", tg_user_id=999,
                  device_name="u999@999", dry_run=False, max_wait=0.1)
    assert any(p[0] == "/api/cli/auth/approve" for p in state.posts)
    row = rt.conn.execute("SELECT * FROM devices WHERE rc_device_id=777").fetchone()
    assert row is not None and row["user_id"] == 999
