"""针对审计收尾的回归：API 改写设备名时的绑定/认领兜底、整期只有未定价请求时的上报。

全部离线：FakeClient / 临时库，不发网络请求。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, enroll, store, syncer                  # noqa: E402
from app.runtime import Runtime                                 # noqa: E402
from test_core import FakeClient, FakeState                     # noqa: E402


@pytest.fixture()
def rt(tmp_path):
    state = FakeState()
    runtime = Runtime(
        db_path=str(tmp_path / "f.db"),
        config={"bot_token": "", "admin_ids": [], "allowed_chat_ids": [],
                "base_url": "https://example.invalid", "dry_run": True},
        client_factory=lambda row: FakeClient(row["id"], state))
    runtime.conn.execute(
        "INSERT INTO rc_accounts(id, label, email, password_enc, status, own_org_id,"
        " capacity, created_at) VALUES ('a1','a1','x@y.z','','ok',1,4,?)",
        (store.iso(),))
    runtime.conn.commit()
    yield runtime, state
    runtime.close()


def _device(rc_id, name):
    return {"id": rc_id, "name": name, "org_id": 1, "sk_prefix": "sk-rec-x",
            "last_used_at": store.now_ms(), "created_at": store.now_ms(),
            "machine_summary": {}, "client_platform": "linux"}


def test_approve_leaves_rewritten_name_for_manual_binding(rt):
    """API 把 name 改成 `reclaude on <host>` 时，不能仅凭唯一性绑定。"""
    runtime, state = rt
    state.devices_next = [_device(777, "reclaude on test-host.local·darwin-arm64")]

    plan = enroll.enroll(runtime.pool, runtime.conn, state="STATE-REWRITE", tg_user_id=999,
                         device_name="u999@999", dry_run=False, max_wait=0.1)

    assert plan["device_id"] is None and plan["pending"] is True
    row = runtime.conn.execute(
        "SELECT user_id FROM devices WHERE account_id='a1' AND rc_device_id=777").fetchone()
    assert row["user_id"] is None


def test_pending_claim_does_not_guess_when_name_rewritten(rt):
    """pending 写入后设备才出现，且 name 被 API 改写时，保留人工绑定。"""
    runtime, state = rt
    plan = enroll.enroll(runtime.pool, runtime.conn, state="STATE-LATE", tg_user_id=999,
                         device_name="u999@999", dry_run=False, max_wait=0)
    assert plan["pending"] is True
    note = json.loads(runtime.conn.execute(
        "SELECT note FROM login_states WHERE state='STATE-LATE'").fetchone()["note"])
    assert note["device_name"].endswith("u999@999")

    runtime.conn.execute(
        "INSERT INTO requests(id, account_id, rc_device_id, created_at, first_seen_at)"
        " VALUES (7001, 'a1', 777, ?, ?)", (store.now_ms() - 3600_000, store.iso()))
    runtime.conn.commit()

    state.devices = [_device(777, "reclaude on test-host.local·darwin-arm64")]
    syncer.sync_account(runtime.pool, runtime.conn, "a1")

    dev = runtime.conn.execute(
        "SELECT user_id FROM devices WHERE account_id='a1' AND rc_device_id=777").fetchone()
    assert dev["user_id"] is None
    assert runtime.conn.execute(
        "SELECT user_id FROM requests WHERE id=7001").fetchone()[0] is None
    ls = runtime.conn.execute(
        "SELECT status, note FROM login_states WHERE state='STATE-LATE'").fetchone()
    assert ls["status"] == "approved_pending_device"


def test_zero_usage_period_still_reports_pending(tmp_path):
    """整期只有 pending（没有已定价用量）时，也要出现在未定价清单里。"""
    conn = store.connect(str(tmp_path / "p.db"))
    store.init(conn)
    conn.execute("INSERT INTO rc_accounts(id, label, email, status, capacity, created_at)"
                 " VALUES ('a1','a1','x@y.z','ok',4,?)", (store.iso(),))
    start, end = date.today() - timedelta(days=1), date.today() + timedelta(days=1)
    conn.execute("INSERT INTO account_periods(account_id, start_date, end_date, cost_cny,"
                 " idle_split, created_at) VALUES ('a1',?,?,100.0,'none',?)",
                 (start.isoformat(), end.isoformat(), store.iso()))
    conn.execute(
        """INSERT INTO requests(id, account_id, rc_device_id, created_at, model, cost_usd,
                                pricing_status, total_tokens, first_seen_at)
           VALUES (1, 'a1', 9001, ?, 'm', NULL, 'pending', 100, ?)""",
        (store.now_ms() - 60_000, store.iso()))
    conn.commit()

    bill = billing.compute(conn, start, end)
    assert bill["users"] == {}
    assert len(bill["anomalies"]["stale"]) == 1
    assert bill["anomalies"]["stale"][0]["requests"] == 1
    zero = [p for p in bill["periods"] if p["user_id"] is None]
    assert zero and "未定价" in zero[0]["note"]
    conn.close()
