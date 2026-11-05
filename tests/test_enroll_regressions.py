"""开通/采集回归测试：席位计数、preferred 硬约束、批准绑定、pending 认领、水位安全。

全部离线：复用 test_core 的 FakeState/FakeClient，不发任何网络请求。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import enroll, store, syncer                              # noqa: E402
from app.runtime import Runtime                                    # noqa: E402
from test_core import FakeClient, FakeState                        # noqa: E402


class PagedClient(FakeClient):
    """history() 按页返回，用来验证 max_pages 截断行为。"""

    def __init__(self, account_id, state, pages):
        super().__init__(account_id, state)
        self.pages = pages

    def history(self, **kw):
        idx = int(kw["cursor"][1:]) if kw.get("cursor") else 0
        if idx < len(self.pages):
            items, next_cursor = self.pages[idx]
            return {"items": list(items), "next_cursor": next_cursor}
        return {"items": [], "next_cursor": None}


@pytest.fixture()
def make_rt(tmp_path):
    created = []

    def _make(name="reg.db", state=None, factory=None):
        state = state if state is not None else FakeState()
        rt = Runtime(
            db_path=str(tmp_path / name),
            config={"bot_token": "", "admin_ids": [], "allowed_chat_ids": [],
                    "base_url": "https://example.invalid", "dry_run": True},
            client_factory=factory or (lambda row: FakeClient(row["id"], state)))
        rt.conn.execute(
            "INSERT INTO rc_accounts(id, label, email, password_enc, status, own_org_id,"
            " capacity, created_at) VALUES ('a1','a1','x@y.z','','ok',1,4,?)",
            (store.iso(),))
        rt.conn.commit()
        created.append(rt)
        return rt, state

    yield _make
    for rt in created:
        rt.close()


def _add_device(conn, rc_device_id, state, *, name=None, user_id=None,
                last_used_at=None, first_seen_at=None):
    conn.execute(
        """INSERT INTO devices(account_id, rc_device_id, user_id, name, state,
                               last_used_at, first_seen_at)
           VALUES ('a1',?,?,?,?,?,?)""",
        (rc_device_id, user_id, name, state, last_used_at, first_seen_at or store.iso()))
    conn.commit()


def _device_dict(rc_device_id, name):
    return {"id": rc_device_id, "name": name, "org_id": 1, "sk_prefix": "sk-rec-x",
            "last_used_at": store.now_ms(), "created_at": store.now_ms(),
            "machine_summary": {}, "client_platform": "linux"}


# a) unknown/missing/revoked 不占席位
def test_used_slots_ignores_unknown_missing_revoked(make_rt):
    rt, _ = make_rt()
    now = store.now_ms()
    for rc_id, state in ((1, "active"), (2, "evicting"), (3, "unknown"),
                         (4, "missing"), (5, "revoked")):
        _add_device(rt.conn, rc_id, state, last_used_at=now - 3_600_000)
    assert enroll.used_slots(rt.conn, "a1") == 2


# b) preferred 是硬约束：账号满且无可下线设备时不能回退到其他账号
def test_pick_account_preferred_is_strict(make_rt):
    rt, _ = make_rt()
    rt.conn.execute(
        "INSERT INTO rc_accounts(id, label, email, password_enc, status, own_org_id,"
        " capacity, created_at) VALUES ('a2','a2','y@y.z','','ok',1,4,?)",
        (store.iso(),))
    rt.conn.commit()
    now = store.now_ms()
    for rc_id in range(1, 5):                       # a1 满员，且都在闲置门槛内
        _add_device(rt.conn, rc_id, "active", last_used_at=now - 60_000)

    acc, victim = enroll.pick_account(rt.conn, preferred="a1")
    assert acc is None and victim is None           # 不能偷偷用有空位的 a2

    acc, victim = enroll.pick_account(rt.conn, preferred="a2")
    assert acc is not None and acc["id"] == "a2" and victim is None

    acc, victim = enroll.pick_account(rt.conn, preferred="does-not-exist")
    assert acc is None and victim is None


# c) 批准后同批出现无关设备 100 时，只能绑 name 匹配的 777
def test_approve_binds_named_device_not_unrelated(make_rt):
    rt, state = make_rt()
    state.devices_next = [_device_dict(100, "old-device"),
                          _device_dict(777, "__approved__")]

    plan = enroll.enroll(rt.pool, rt.conn, state="STATE-APPROVE", tg_user_id=999,
                         device_name="u999@999", dry_run=False, max_wait=0.1)

    assert plan["device_id"] == 777 and plan["pending"] is False
    row777 = rt.conn.execute(
        "SELECT * FROM devices WHERE account_id='a1' AND rc_device_id=777").fetchone()
    row100 = rt.conn.execute(
        "SELECT * FROM devices WHERE account_id='a1' AND rc_device_id=100").fetchone()
    assert row777 is not None and row777["user_id"] == 999
    assert row100 is not None and row100["user_id"] is None
    ls = rt.conn.execute(
        "SELECT * FROM login_states WHERE state='STATE-APPROVE'").fetchone()
    assert ls["status"] == "approved" and ls["note"] == "device_id=777"


# d) 超时写 pending，设备晚到后由下一轮 sync 自动认领并回填历史流水
def test_pending_login_claimed_by_sync(make_rt):
    rt, state = make_rt()
    device_name = "u999@999"
    plan = enroll.enroll(rt.pool, rt.conn, state="STATE-PENDING", tg_user_id=999,
                         device_name=device_name, dry_run=False, max_wait=0)
    assert plan["pending"] is True and plan["device_id"] is None

    ls = rt.conn.execute(
        "SELECT * FROM login_states WHERE state='STATE-PENDING'").fetchone()
    assert ls["status"] == "approved_pending_device"
    note = json.loads(ls["note"])
    assert note["device_name"].startswith('reclaude on test-laptop · linux-amd64')
    assert device_name not in note["device_name"]
    assert note['marker'] in note['device_name']
    approve = next(body for path,body in state.posts if path.endswith('/approve'))
    assert approve['device_name'] == note['device_name']
    assert isinstance(note["approved_at_ms"], int)

    # 远早于本次 sync 窗口的历史流水：只有 claim 的 UPDATE 才能补上归属
    old_ts = store.now_ms() - 48 * 3_600_000
    rt.conn.execute(
        """INSERT INTO requests(org_id,id, account_id, rc_device_id, created_at, first_seen_at)
           VALUES (1,9001, 'a1', 777, ?, ?)""",
        (old_ts, store.iso()))
    rt.conn.commit()

    state.devices = [_device_dict(777, note["device_name"])]     # 设备这一刻才出现
    syncer.sync_account(rt.pool, rt.conn, "a1")

    dev = rt.conn.execute(
        "SELECT * FROM devices WHERE account_id='a1' AND rc_device_id=777").fetchone()
    assert dev is not None and dev["user_id"] == 999
    assert rt.conn.execute("SELECT user_id FROM requests WHERE id=9001").fetchone()[0] == 999
    ls = rt.conn.execute(
        "SELECT * FROM login_states WHERE state='STATE-PENDING'").fetchone()
    assert ls["status"] == "approved" and ls["note"] == "device_id=777"

    # 幂等：再同步一次不会重复认领/重复记事件
    syncer.sync_account(rt.pool, rt.conn, "a1")
    assert rt.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='approve_claimed'").fetchone()[0] == 1


# e) 显式 since_ms 的窄窗采集不能推进水位；整窗采集才可以
def test_explicit_since_does_not_advance_watermark(make_rt):
    rt, state = make_rt()
    old = store.now_ms() - 3_600_000
    rt.conn.execute(
        "INSERT INTO watermarks(account_id, last_synced_at, last_request_id, updated_at)"
        " VALUES ('a1',?,NULL,?)", (old, store.iso()))
    rt.conn.commit()
    state.add_item(1, 100, 1.0)

    syncer.sync_account(rt.pool, rt.conn, "a1", since_ms=store.now_ms() - 5 * 60_000)
    wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id='a1'").fetchone()
    assert wm["last_synced_at"] == old

    full = syncer.sync_account(rt.pool, rt.conn, "a1")
    wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id='a1'").fetchone()
    assert wm["last_synced_at"] == full["until"]
    assert wm["last_synced_at"] > old


# f) 整窗采集被 max_pages 截断时不得推进水位，且必须报告 truncated=True
def test_truncated_full_sync_keeps_watermark(make_rt):
    state = FakeState()
    state.add_item(1, 100, 1.0)
    state.add_item(2, 100, 1.0)
    pages = [([state.items[0]], "c1"), ([state.items[1]], None)]
    rt, state = make_rt("paged.db", state=state,
                        factory=lambda row: PagedClient(row["id"], state, pages))

    old = store.now_ms() - 3_600_000
    rt.conn.execute(
        "INSERT INTO watermarks(account_id, last_synced_at, last_request_id, updated_at)"
        " VALUES ('a1',?,NULL,?)", (old, store.iso()))
    rt.conn.commit()

    out = syncer.sync_account(rt.pool, rt.conn, "a1", max_pages=1)
    assert out["truncated"] is True and out["pages"] == 1
    wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id='a1'").fetchone()
    assert wm["last_synced_at"] == old

    # 下一轮拉完剩下的页后才推进
    out2 = syncer.sync_account(rt.pool, rt.conn, "a1", max_pages=5)
    assert out2["truncated"] is False
    wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id='a1'").fetchone()
    assert wm["last_synced_at"] == out2["until"] > old
