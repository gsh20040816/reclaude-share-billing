"""Git Bash integration check with a local allocation endpoint and mock ReClaude."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = Path(__file__).resolve().parents[1]


class Endpoint(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if (self.headers["Authorization"] != "Bearer test-key" or request["model"] != "fable"
                or request["action"] != "plan" or request["device"] != 10):
            self.send_error(400)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"org_id":2,"account_id":"second","reuse":20}')


def smoke(root):
    (root / "rerec").write_bytes((ROOT / "rerec/rerec").read_bytes())
    rec = root / "mock-rec.cmd"
    rec.write_text('@echo off\r\npython "%~dp0mock.py" %*\r\n')
    (root / "mock.py").write_text("""
import json, pathlib, sys
p = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args == ['status','--check','--json']:
    bindings = [dict(org_id=1,device_id=10),dict(org_id=2,device_id=20)]
    idx = int((p/'active').read_text())
    print(json.dumps(dict(claude=dict(login=dict(state='valid'),current=bindings[idx],bindings=bindings),
                          **{'global': {'data_dir':str(p)}})))
elif args == ['org','use','2']: (p/'active').write_text('1')
elif args[0]=='cli': (p/'launch').write_text(json.dumps(args[1:]))
else: sys.exit(2)
""")
    (root / "active").write_text("0")
    before, after = [root / "claude" / "accounts" / str(i) / "config" for i in (10, 20)]
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    (before / "history.jsonl").write_text("你好\n", encoding="utf-8")
    (before / ".claude.json").write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}}))
    endpoint = HTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    bash = Path(os.environ['ProgramFiles']) / 'Git/bin/bash.exe'
    env = dict(os.environ, REREC_URL=f"http://127.0.0.1:{endpoint.server_port}", REREC_REC=str(rec),
               XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'))
    def run(*args, **kwargs):
        return subprocess.run([bash, "./rerec", *args], cwd=root, env=env, check=True, timeout=90, **kwargs)
    try:
        run("pair", input=b"test-key\n")
        run("login")
        run("switch", "fable")
        assert json.loads((root / "launch").read_text()) == ["--model", "fable"]
        assert (after / "history.jsonl").read_text(encoding="utf-8") == "你好\n"
        assert json.loads((after / ".claude.json").read_text())["projects"][str(root)]["hasTrustDialogAccepted"]
    finally:
        endpoint.shutdown()
        endpoint.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="rerec 测试 ") as directory:
        smoke(Path(directory))
