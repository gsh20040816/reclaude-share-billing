"""Bot 渲染 / 命令路径离线测试：全部走 FakeClient，不发任何网络请求。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store
from app.bot import Bot
from app.runtime import Runtime
from test_core import FakeClient, FakeState

ADMIN = 2039687026


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


def test_help_and_accounts(bot):
    bot.command(_msg()["chat"], _msg(), ADMIN, "/help", None)
    bot.command(_msg()["chat"], _msg(), ADMIN, "/accounts", None)
    assert any("账号池" in s for s in bot.tg.sent)
    assert any("租期" in s for s in bot.tg.sent)


def test_account_add_flow_encrypts_and_deletes_password(bot):
    chat = _msg()["chat"]
    bot.command(chat, _msg(), ADMIN, "/account_add", None)
    for text in ("ops@example.com", "s3cret-pw", "ultra-a"):
        msg = _msg(text=text)
        bot.handle({"message": msg})
    assert 7 in bot.tg.deleted                                     # 密码消息被删
    row = bot.conn.execute("SELECT * FROM rc_accounts WHERE id='ultra-a'").fetchone()
    assert row is not None and row["password_enc"].startswith("fernet:")
    assert bot.conn.execute("SELECT COUNT(*) FROM account_periods").fetchone()[0] == 0


def test_report_and_rates_render(bot):
    chat = _msg()["chat"]
    bot.command(chat, _msg(), ADMIN, "/rates", None)
    bot.command(chat, _msg(), ADMIN, "/report", None)
    bot.command(chat, _msg(), ADMIN, "/health", None)
    assert any("模型倍率" in s for s in bot.tg.sent)
    assert any("账单" in s for s in bot.tg.sent)
    assert any("健康" in s for s in bot.tg.sent)


def test_user_cannot_run_admin_commands(bot):
    bot.allowed.add(555)
    chat = _msg(uid=555)["chat"]
    bot.command(chat, _msg(uid=555), 555, "/account_add", None)
    assert any("仅管理员" in s for s in bot.tg.sent)


def test_login_dry_run_flow(bot):
    chat = _msg()["chat"]
    bot.command(chat, _msg(), ADMIN, "/login https://www.reclaude.ai/cli/auth?state=ABCDEFGH123", None)
    assert any("演练" in s or "登记" in s for s in bot.tg.sent)
