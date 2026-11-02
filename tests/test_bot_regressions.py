"""Bot 回归测试：权限、账号添加回滚、采集告警去抖、报表/健康统计。

全部离线：Runtime + tmp db + StubTelegram + FakeClient，不发任何网络请求。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.bot as botmod                                # noqa: E402
from app import billing, cli, store                     # noqa: E402
from app.bot import Bot                                 # noqa: E402
from app.runtime import Runtime                         # noqa: E402
from test_core import FakeClient, FakeState             # noqa: E402

ADMIN = 2039687026
USER = 555


class StubTelegram:
    def __init__(self):
        self.sent = []
        self.deleted = []

    def send(self, chat_id, text, thread_id=None, markup=None):
        self.sent.append(text)
        return {"ok": True}

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return {"ok": True}

    def call(self, method, **params):
        return {"ok": True, "result": []}


@pytest.fixture()
def bot(tmp_path):
    state = FakeState()
    rt = Runtime(db_path=str(tmp_path / "b.db"),
                 config={"bot_token": "TEST", "admin_ids": [ADMIN], "allowed_chat_ids": [ADMIN],
                         "base_url": "https://example.invalid", "dry_run": True},
                 client_factory=lambda row: FakeClient(row["id"], state))
    rt.conn.execute("INSERT INTO rc_accounts(id,label,email,password_enc,status,own_org_id,capacity,created_at)"
                    " VALUES ('a1','a1','x@y.z','','ok',1,4,?)", (store.iso(),))
    rt.conn.commit()
    b = Bot(rt)
    b.tg = StubTelegram()
    yield b
    rt.close()


def _msg(uid=ADMIN, text="/x"):
    return {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "username": "gsh"},
            "message_id": 7, "text": text}


def _run_account_add(bot, uid, email, password, label, start, end, cost):
    bot.command(_msg(uid=uid)["chat"], _msg(uid=uid), uid, "/account_add", None)
    for text in (email, password, label, start, end, cost):
        bot.handle({"message": _msg(uid=uid, text=text)})


# ---------------------------------------------------------------- 1) /health 权限
def test_health_is_admin_only(bot):
    bot.allowed.add(USER)
    bot.command(_msg(uid=USER)["chat"], _msg(uid=USER), USER, "/health", None)
    assert any("仅管理员" in s for s in bot.tg.sent)
    assert not any("健康" in s for s in bot.tg.sent)

    bot.tg.sent.clear()
    bot.command(_msg()["chat"], _msg(), ADMIN, "/health", None)
    assert any("健康" in s for s in bot.tg.sent)


# ---------------------------------------------------------------- 2) /account_add 回滚
def test_account_add_invalid_date_leaves_no_rows(bot):
    _run_account_add(bot, ADMIN, "ops@example.com", "pw", "bad-date-account",
                     "2026-11-01", "not-a-date", "2200")

    assert store.account_row(bot.conn, "bad-date-account") is None
    assert bot.conn.execute("SELECT COUNT(*) n FROM account_periods").fetchone()["n"] == 0
    assert ADMIN not in bot.flows
    assert any("格式不对" in s for s in bot.tg.sent)


def test_account_add_rolls_back_when_period_insert_fails(bot, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("与已有租期重叠：2026-11-01 ~ 2026-11-30")

    monkeypatch.setattr(botmod.billing, "add_period", boom)
    _run_account_add(bot, ADMIN, "ops@example.com", "pw", "half-created",
                     "2026-11-01", "2026-11-30", "2200")

    assert store.account_row(bot.conn, "half-created") is None
    assert bot.conn.execute("SELECT COUNT(*) n FROM account_periods").fetchone()["n"] == 0
    assert ADMIN not in bot.flows
    assert any("已回滚" in s for s in bot.tg.sent)


# ---------------------------------------------------------------- 3) 采集失败告警去抖
def test_sync_alerts_once_and_recovers(bot):
    fail = {"account_id": "a1", "error": "boom"}

    for _ in range(2):
        bot._handle_sync_results([fail])
    assert not any("采集连续失败" in s for s in bot.tg.sent)

    bot._handle_sync_results([fail])                     # 第 3 次：告警
    assert sum("采集连续失败" in s for s in bot.tg.sent) == 1

    bot._handle_sync_results([fail])                     # 第 4 次：保持安静
    assert sum("采集连续失败" in s for s in bot.tg.sent) == 1

    bot._handle_sync_results([{"account_id": "a1", "inserted": 1}])   # 恢复
    assert sum("采集已恢复" in s for s in bot.tg.sent) == 1

    bot._handle_sync_results([{"account_id": "a1", "inserted": 1}])   # 不重复恢复
    assert sum("采集已恢复" in s for s in bot.tg.sent) == 1


# ---------------------------------------------------------------- 4) /report 未定价明细
def _add_pending_request(conn, rid, created_at):
    conn.execute(
        """INSERT INTO requests(id, account_id, rc_device_id, created_at, model, cost_usd,
                                pricing_status, total_tokens, first_seen_at)
           VALUES (?, 'a1', 100, ?, 'claude-opus-5-5', NULL, 'pending', 100, ?)""",
        (rid, created_at, store.iso()))
    conn.commit()


def test_report_renders_stale_count_suffix(bot):
    today = date.today()
    billing.add_period(bot.conn, "a1", today - timedelta(days=3), today + timedelta(days=1), 1000.0)
    now = store.now_ms()
    old = billing.day_start_ms(today - timedelta(days=3))
    # 有定价用量，compute 才会走到 pending 统计（整期零用量的租期会提前 continue）。
    bot.conn.execute(
        """INSERT INTO requests(id, account_id, rc_device_id, created_at, model, cost_usd,
                                pricing_status, total_tokens, first_seen_at)
           VALUES (9000, 'a1', 100, ?, 'claude-opus-5-5', 1.0, 'priced', 100, ?)""",
        (now - 3_600_000, store.iso()))
    _add_pending_request(bot.conn, 9001, old + 3_600_000)
    _add_pending_request(bot.conn, 9002, old + 2 * 3_600_000)
    _add_pending_request(bot.conn, 9003, now - 3_600_000)

    bot.command(_msg()["chat"], _msg(), ADMIN, "/report", None)
    report = bot.tg.sent[-1]
    assert "未定价" in report
    assert "3 条（其中超 24h 2 条）" in report


def test_report_without_stale_key_does_not_crash(bot, monkeypatch):
    def fake_compute(conn, start=None, end=None):
        return {"start": "2026-11-01", "end": "2026-11-30", "periods": [], "users": {},
                "anomalies": {"incomplete": [], "uncovered": [],
                              "unrated_models": {}}}

    monkeypatch.setattr(botmod.billing, "compute", fake_compute)
    bot.command(_msg()["chat"], _msg(), ADMIN, "/report", None)
    assert any("账单" in s for s in bot.tg.sent)


# ---------------------------------------------------------------- 5) /health 未定价计数
def test_health_unpriced_count_excludes_ignored(bot):
    bot.conn.executemany(
        """INSERT INTO requests(id, account_id, rc_device_id, created_at, model, cost_usd,
                                pricing_status, total_tokens, first_seen_at)
           VALUES (?, 'a1', 100, ?, 'claude-opus-5-5', ?, ?, 100, ?)""",
        [(9101, store.now_ms(), 0.5, "priced", store.iso()),
         (9102, store.now_ms(), None, "ignored", store.iso()),
         (9103, store.now_ms(), None, "pending", store.iso())])
    bot.conn.commit()

    bot.command(_msg()["chat"], _msg(), ADMIN, "/health", None)
    health = next(s for s in bot.tg.sent if "健康" in s)
    assert "未定价 1 条" in health


# ---------------------------------------------------------------- CLI account-add 回滚
def _cli_args(db, **overrides):
    base = {"db": db, "id": "cli-a", "email": "x@y.z", "password": "",
            "start": "2026-11-01", "end": "2026-11-30", "cost": "2200"}
    base.update(overrides)
    return argparse.Namespace(**base)


def _assert_cli_account_absent(db, account_id):
    conn = store.connect(db)
    try:
        assert conn.execute("SELECT id FROM rc_accounts WHERE id=?",
                            (account_id,)).fetchone() is None
        assert conn.execute("SELECT COUNT(*) n FROM account_periods WHERE account_id=?",
                            (account_id,)).fetchone()["n"] == 0
    finally:
        conn.close()


def test_cli_account_add_invalid_date_leaves_no_row(tmp_path):
    db = str(tmp_path / "cli.db")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_account_add(_cli_args(db, end="not-a-date"))
    assert "格式" in str(exc.value)
    _assert_cli_account_absent(db, "cli-a")


def test_cli_account_add_rolls_back_when_period_fails(tmp_path, monkeypatch):
    db = str(tmp_path / "cli.db")

    def boom(*args, **kwargs):
        raise ValueError("与已有租期重叠")

    monkeypatch.setattr(cli.billing, "add_period", boom)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_account_add(_cli_args(db))
    assert "添加账号失败" in str(exc.value)
    _assert_cli_account_absent(db, "cli-a")
