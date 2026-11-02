"""对账测试：未归属用量留在「待认领」、ignored/not_billable 完全排除、重复出账覆盖快照。

全部离线：合成库用 tmp_path 临时文件；真实库测试在 /tmp/rc-verify.db 不存在时跳过。
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import billing, store                                   # noqa: E402

REAL_DB = Path("/tmp/rc-verify.db")


# ------------------------------------------------------------------ 造数
def _db(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init(conn)
    return conn


def _account(conn, aid="a1"):
    from app import ledger
    ledger.ensure_unit(conn, 1, 101)
    conn.execute("INSERT INTO rc_accounts(id, label, email, status, capacity, created_at)"
                 " VALUES (?,?,?,'ok',4,?)", (aid, aid, "x@y.z", store.iso()))
    conn.commit()


def _period(conn, start, end, cost, aid="a1"):
    conn.execute("INSERT INTO account_periods(billing_unit_id,account_id, start_date, end_date, cost_cny,"
                 " idle_split, created_at) VALUES ('1:seat1',?,?,?,?,'none',?)",
                 (aid, start.isoformat(), end.isoformat(), cost, store.iso()))
    conn.commit()


def _req(conn, rid, device, user_id, created_at, cost_usd, status, aid="a1",
         model="claude-haiku-4-5-20251001"):
    conn.execute(
        """INSERT INTO requests(billing_unit_id,id, account_id, rc_device_id, user_id, created_at, model,
                                cost_usd, pricing_status, total_tokens, first_seen_at)
           VALUES ('1:seat1',?,?,?,?,?,?,?,?,?,?)""",
        (rid, aid, device, user_id, created_at, model, cost_usd, status, 1000, store.iso()))
    conn.commit()


# ------------------------------------------------------------------ 1) 未归属份额不转嫁
def test_unassigned_usage_held_as_suspense(tmp_path):
    conn = _db(tmp_path)
    start, end = date.today() - timedelta(days=1), date.today() + timedelta(days=1)
    _account(conn)
    _period(conn, start, end, 100.0)
    ts = store.now_ms() - 60_000
    _req(conn, 1, 9001, 111, ts, 10.0, "priced")
    _req(conn, 2, 9002, None, ts, 10.0, "priced")       # 未归属设备

    bill = billing.compute(conn, start, end)

    # 分母是 20 而不是 10：未归属那半个份额不能被摊到 111 头上
    assert bill["users"][111] == 50.00
    unassigned = bill["anomalies"]["unassigned"]
    assert len(unassigned) == 1
    assert unassigned[0]["amount_cny"] == 50.00
    total = sum(bill["users"].values()) + sum(a["amount_cny"] for a in unassigned)
    assert round(total, 2) == 100.00

    suspense = [p for p in bill["periods"] if p["user_id"] is None]
    assert len(suspense) == 1
    assert suspense[0]["amount_cny"] == 50.00
    assert "待认领" in suspense[0]["note"]

    # 同一区间重复落账：覆盖旧快照，不产生第二套账单
    first = billing.create_invoices(conn, bill)
    assert len(first) == 1
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0] == 1
    second = billing.create_invoices(conn, bill)
    assert len(second) == 1
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0] == 1
    # 待认领份额不进个人账单
    assert conn.execute("SELECT SUM(total_cny) FROM invoices").fetchone()[0] == 50.00
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE user_id IS NULL").fetchone()[0] == 0


# ------------------------------------------------------------------ 2) ignored/not_billable
def test_ignored_and_not_billable_excluded_everywhere(tmp_path):
    conn = _db(tmp_path)
    start, end = date.today() - timedelta(days=1), date.today() + timedelta(days=1)
    _account(conn)
    _period(conn, start, end, 100.0)
    ts = store.now_ms() - 60_000
    _req(conn, 1, 9001, 111, ts, 1.0, "priced")
    _req(conn, 2, 9002, None, ts, None, "ignored")
    _req(conn, 3, 9003, None, ts, None, "not_billable")

    bill = billing.compute(conn, start, end)

    assert bill["anomalies"]["stale"] == []
    assert bill["anomalies"]["unassigned"] == []
    assert bill["users"][111] == 100.00

    s_ms, e_ms = billing.day_start_ms(start), billing.day_end_ms(end)
    usage = billing.weighted_usage(conn, "1:seat1", s_ms, e_ms)
    assert usage["unassigned_weighted"] == 0.0
    assert usage["unassigned_requests"] == 0
    assert list(usage["per_user"]) == [111]
    assert usage["per_user"][111]["weighted"] == 1.0

    assert billing.pending_stats(conn, "1:seat1", s_ms, e_ms) == \
        {"requests": 0, "tokens": 0, "stale": 0}


# ------------------------------------------------------------------ 3) 真实库（可选）
@pytest.mark.skipif(not REAL_DB.exists(), reason="/tmp/rc-verify.db 不存在，跳过真实对账")
def test_real_data_reconciliation(tmp_path):
    db = tmp_path / "rc-verify.db"
    # 用 sqlite backup 而不是 copy2：真实库是 WAL 模式，可能还有未 checkpoint 的内容
    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(str(db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    conn = store.connect(str(db))
    original_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    store.init(conn)
    bill = billing.compute(conn, date(2026, 10, 27), date(2026, 11, 26))
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == original_count
    assert bill["users"] == {}  # Old export has no upstream account IDs; never guess.
    assert bill["anomalies"]["legacy_periods"]
    conn.close()
