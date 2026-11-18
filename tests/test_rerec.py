import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.request

import pytest

from app import allocation, evictor, rerec, store
from app.rerec_http import serve
from test_bot import bot, ADMIN
from test_core import FakeClient, env

ROOT = Path(__file__).resolve().parents[1]


def upstream(overall=50, scoped=99):
    return dict(
        usage_updated_at=store.now_ms(),
        usage_snapshot={
            "five_hour": {"utilization": 5},
            "seven_day": {"utilization": overall},
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "percent": scoped,
                    "scope": {"model": {"display_name": "Fable"}},
                }
            ],
        },
    )


def test_model_windows():
    account = upstream(scoped=100)
    assert rerec.windows(account, "opus", store.now_ms()) == 50
    with pytest.raises(ValueError, match="fable"):
        rerec.windows(account, "claude-fable-5-2", store.now_ms())
    account["usage_snapshot"]["five_hour"]["utilization"] = 100
    with pytest.raises(ValueError, match="5h"):
        rerec.windows(account, "opus", store.now_ms())


def test_candidates_and_flush_recheck(env, monkeypatch):
    rt, state = env
    rt.cfg["dry_run"] = False
    uid = 111
    for org, did, owner in [(9, 90, uid), (1, 42, 222)]:
        rt.conn.execute("INSERT INTO device_bindings VALUES(?,?,?)", (org, did, owner))
    rt.conn.execute(
        "INSERT INTO devices(account_id,rc_device_id,org_id,state) VALUES('a1',42,1,'active')"
    )
    state.devices = [dict(id=42, org_id=1)]
    state.add_item(1, 42, 1, created_at=store.now_ms() - 7 * 3600_000)
    from app import syncer

    syncer._upsert_request(rt.conn, "a1", state.items[0])
    rt.conn.commit()
    c = dict(
        org_id=1,
        account_id="a1",
        account=upstream(),
        devices=state.devices,
        login_full=True,
        org_full=True,
        available=False,
        normalized_usage=0,
    )
    monkeypatch.setattr(allocation, "inventory", lambda *a: ([c], []))
    with pytest.raises(ValueError, match="绑定"):
        rerec.pick(rt, 999, "fable", 9, 90)
    assert rerec.pick(rt, uid, "fable", 9, 90)["victim"]["id"] == 42
    # This request becomes visible only in the final directed sync. 4h is
    # outside the old 90-minute guard but inside rerec's 6-hour guard.
    state.add_item(2, 42, 1, created_at=store.now_ms() - 4 * 3600_000)
    args = dict(
        action="approve",
        model="fable",
        source=9,
        device=90,
        state="new-state1",
        account="a1",
        org=1,
        victim=rerec.pick(rt, uid, "fable", 9, 90)["victim"],
    )
    with pytest.raises(evictor.EvictionError, match="没有符合条件"):
        rerec.handle(SimpleNamespace(rt=rt, allowed_chat=lambda *args: True), uid, args)
    assert not any(path.endswith("/revoke") for path, _ in state.posts)

    # Once the second request also ages out, exactly one removal creates room.
    rt.conn.execute("UPDATE requests SET created_at=? WHERE id=2", (store.now_ms() - 7 * 3600_000,))
    rt.conn.commit()
    original = FakeClient.post

    def post(client, path, body=None):
        result = original(client, path, body)
        if path.endswith("/revoke"):
            state.devices.clear()
            c["available"] = True
        return result

    monkeypatch.setattr(FakeClient, "post", post)
    assert rerec.handle(SimpleNamespace(rt=rt, allowed_chat=lambda *a: True), uid, args)["approved"]
    writes = [path for path, _ in state.posts if path.endswith(("/approve", "/revoke"))]
    assert len(writes) == 2 and writes[0].endswith("/revoke") and writes[1].endswith("/approve")


def test_http_pair_plan_and_approve(bot):
    token = rerec.issue_token(bot.conn, ADMIN)
    bot.cfg["dry_run"] = False
    server = serve(bot, 0)

    def call(data):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/",
            json.dumps(data).encode(),
            {"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)

    try:
        args = dict(model="", source=0, device=0)
        plan = call(dict(args, action="plan"))
        data = dict(
            args, action="approve", state="abcdefgh123", account=plan["account_id"], org=plan["org_id"]
        )
        assert call(data)["approved"]
        assert call(data)["approved"]
        assert (
            bot.conn.execute(
                "SELECT count(*) FROM login_states WHERE state=?", (data["state"],)
            ).fetchone()[0]
            == 1
        )
    finally:
        server.shutdown()
        server.server_close()
        bot.stop()


def test_bash_switch_migrates_and_launches(tmp_path, bot, monkeypatch):
    rt = bot.rt
    source, target = tmp_path / "rec/claude/accounts/90", tmp_path / "rec/claude/accounts/91"
    for folder in [source, target]:
        (folder / "config").mkdir(parents=True)
    (source / "config/.claude.json").write_text(
        json.dumps({"projects": {"/work": {"hasTrustDialogAccepted": True, "x": 7}}})
    )
    (target / "config/.claude.json").write_text(
        json.dumps({"auth": "target", "projects": {"/work": {"y": 8}}})
    )
    (source / "config/history.jsonl").write_text("a session\n")
    shared = tmp_path / ".claude"
    shared.mkdir()
    (shared / "CLAUDE.md").write_text("common")
    (source / "config/CLAUDE.md").write_text("old rules")
    for name in ("mine", "synced"):
        (source / "config/skills" / name).mkdir(parents=True)
        (source / "config/skills" / name / "SKILL.md").write_text(name)
    memory = Path("projects/work/memory")
    (source / "config" / memory).mkdir(parents=True)
    (source / "config" / memory / "MEMORY.md").write_text("project")
    active = tmp_path / "current"
    active.write_text("90")
    rec = tmp_path / "fake-rec"
    rec.write_text("""#!/usr/bin/env python3
import json,os,sys,pathlib
d=pathlib.Path(os.environ['TEST_DIR'])
current=d/'current'
if sys.argv[1]=='status':
    bindings=[{'org_id':9,'device_id':90},{'org_id':1,'device_id':91}]
    print(json.dumps({'global':{'data_dir':str(d/'rec')},'claude':{'bindings':bindings,'login':{'state':'valid'},
                      'current':next(b for b in bindings if b['device_id']==int(current.read_text()))}}))
elif sys.argv[1:]==['org','use','1']:current.write_text('91')
elif sys.argv[1]=='cli':(d/'launch').write_text(json.dumps(sys.argv[2:]))
else:sys.exit(2)
""")
    rec.chmod(0o755)
    # Process enumeration is isolated from the developer's real sessions.
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\nexit 0\n")
    ps.chmod(0o755)
    for org, did in [(9, 90), (1, 91)]:
        rt.conn.execute("INSERT INTO device_bindings VALUES(?,?,?)", (org, did, ADMIN))
    rt.conn.commit()
    c = dict(
        org_id=1,
        account_id="a1",
        account=upstream(),
        devices=[dict(id=91, org_id=1)],
        login_full=True,
        org_full=True,
        available=False,
        normalized_usage=0,
    )
    monkeypatch.setattr(allocation, "inventory", lambda *args: ([c], []))
    token = rerec.issue_token(rt.conn, ADMIN)
    server = serve(bot, 0)
    config = tmp_path / "config/rerec"
    config.mkdir(parents=True)
    (config / "client.json").write_text(
        json.dumps(dict(token=token, url=f"http://127.0.0.1:{server.server_port}/"))
    )
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TEST_DIR=str(tmp_path),
        REREC_REC=str(rec),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
    )
    try:
        subprocess.run([ROOT / "rerec/rerec", "switch", "fable"], env=env, check=True, timeout=15)
        for account in (source, target):
            assert (account / "config/CLAUDE.md").readlink() == shared / "CLAUDE.md"
            assert (account / "config/skills/mine").readlink() == shared / "skills/mine"
        assert next(source.glob(".rerec-link-backup-*/CLAUDE.md")).read_text() == "old rules"
        assert not (shared / "skills/synced").exists()
        assert not (target / "config" / memory).is_symlink()
        assert (target / "config" / memory / "MEMORY.md").read_text() == "project"
        (config / "migration.json").write_text(json.dumps({"links": ["skills"]}))
        subprocess.run([ROOT / "rerec/rerec", "login"],env=env,check=True)
        assert not (target / "config/CLAUDE.md").is_symlink()
        assert (target / "config/CLAUDE.md").read_text() == "common"
        assert (target / "config/history.jsonl").read_text() == "a session\n"
        assert json.loads((target / "config/.claude.json").read_text()) == {
            "auth": "target",
            "projects": {"/work": {"y": 8, "hasTrustDialogAccepted": True}},
        }
        assert json.loads((tmp_path / "launch").read_text()) == ["--model", "fable"]
    finally:
        server.shutdown()
        server.server_close()
        bot.stop()


def test_first_login_bash_authorizes_once(tmp_path, bot):
    bot.cfg["dry_run"] = False
    server = serve(bot, 0)
    token = rerec.issue_token(bot.conn, ADMIN)
    config = tmp_path / "config/rerec"
    config.mkdir(parents=True)
    (config / "client.json").write_text(
        json.dumps(dict(token=token, url=f"http://127.0.0.1:{server.server_port}/"))
    )
    rec = tmp_path / "rec"
    rec.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['FAKE_CURRENT'])
if sys.argv[1]=='status':
    current={'org_id':1,'device_id':123} if p.exists() else {}
    print(json.dumps({'global':{'data_dir':str(p.parent/'data')},'claude':{'current':current,'bindings':[current] if current else [],
                     'login':{'state':'valid' if current else 'none'}}}))
elif sys.argv[1]=='login':
    print('https://www.reclaude.ai/cli/auth?state=my-test-state',flush=True)
    (p.parent/'data/claude/accounts/123/config').mkdir(parents=True)
    p.write_text('123')
elif sys.argv[1]=='cli':
    print('launched')
else:sys.exit(2)
""")
    rec.chmod(0o755)
    env = dict(
        os.environ,
        REREC_REC=str(rec),
        HOME=str(tmp_path),
        FAKE_CURRENT=str(tmp_path / "current"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
    )
    try:
        result = subprocess.run(
            [ROOT / "rerec/rerec"], env=env, check=True, timeout=15, capture_output=True, text=True
        )
        assert result.stdout.strip() == "launched"
        assert (
            bot.conn.execute(
                "SELECT tg_user_id FROM login_states WHERE state=?", ("my-test-state",)
            ).fetchone()[0]
            == ADMIN
        )
        (config / "client.json").unlink()
        subprocess.run([ROOT / "rerec/rerec"], env=env, check=True, timeout=15)
    finally:
        server.shutdown()
        server.server_close()
        bot.stop()


def test_release_must_free_every_full_scope():
    a = dict(account_id="a", org_id=1, devices=[dict(id=10, org_id=1), dict(id=20, org_id=2)])
    b = dict(account_id="b", org_id=2, devices=[dict(id=30, org_id=1)])
    for login_full, org_full, want in [
        (True, True, {10}),
        (True, False, {10, 20}),
        (False, True, {10, 30}),
    ]:
        target = dict(a, login_full=login_full, org_full=org_full)
        assert {d["id"] for _, d in rerec.releasable([a, b], target)} == want
