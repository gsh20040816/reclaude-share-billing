"""ReClaude 后台 API 客户端（多账号会话池）。

事实来源：2026-11 实测 www.reclaude.ai 前端 bundle + 真实接口返回。
    GET  /api/app/devices                    设备列表（含 sk_prefix / last_used_at / revoked_at）
    GET  /api/app/devices/quota              设备名额（max / used / account_max / account_used）
    GET  /api/app/usage/stats?range=&device_id=&org_id=
                                             用量：overview(累计 usd/tokens) + heatmap(按天)
    POST /api/app/devices/{id}/revoke        下线设备（立即失效）
    POST /api/cli/auth/describe {state}      reclaude login 的授权请求详情
    POST /api/cli/auth/approve  {state, device_name, org_id}   批准登录 = 分配设备名额
    POST /api/app/account/usage/refresh      刷新官方 /usage 快照（写操作，可选）
    GET  /api/app/me, /api/app/orgs/         订阅、额度、组织

鉴权：会话 cookie（/api/auth/login 拿到的 rc_sid）。服务端每次响应都会轮转 cookie，
所以每个账号的 cookie 必须在每次请求后回写，不能手工并发复用同一份。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request

BASE_DEFAULT = "https://www.reclaude.ai"
UA = "reclaude-billing/0.1 (+ops)"


class RcError(Exception):
    pass


class RcClient:
    """单账号客户端。cookie 由调用方在每次请求后落盘（rotate 语义）。"""

    def __init__(self, base_url: str = BASE_DEFAULT, cookie: str | None = None):
        self.base = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        if cookie:
            self._load_cookie(cookie)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("Accept", "application/json"), ("User-Agent", UA)]

    # ---------------------------------------------------------------- cookie
    def _load_cookie(self, cookie: str) -> None:
        host = urllib.parse.urlsplit(self.base).hostname or "www.reclaude.ai"
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.strip().split("=", 1)
            self.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=k, value=v, port=None, port_specified=False,
                domain=host, domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True, secure=True, expires=None,
                discard=True, comment=None, comment_url=None, rest=None))

    @property
    def cookie(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    # ------------------------------------------------------------------ http
    def request(self, method: str, path: str, body=None, timeout: int = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw, status = resp.read().decode("utf-8", "replace"), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode("utf-8", "replace"), exc.code
        except Exception as exc:                      # 网络层
            raise RcError(f"{type(exc).__name__}: {exc}") from exc
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {"_raw": raw[:500]}
        return status, payload

    def get(self, path: str):
        status, body = self.request("GET", path)
        self._raise(status, body, path)
        return body

    def post(self, path: str, body=None):
        status, payload = self.request("POST", path, body if body is not None else {})
        self._raise(status, payload, path)
        return payload

    @staticmethod
    def _raise(status: int, body, path: str) -> None:
        if status == 200:
            return
        if status == 401:
            raise RcError(f"{path}: 未登录/会话失效 (401)")
        raise RcError(f"{path}: HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")

    # ------------------------------------------------------------------ auth
    def login(self, email: str, password: str) -> dict:
        status, body = self.request("POST", "/api/auth/login", {"email": email, "password": password})
        if status != 200:
            raise RcError(f"登录失败 HTTP {status}: {json.dumps(body, ensure_ascii=False)[:300]}")
        if isinstance(body, dict) and body.get("mfa_required"):
            raise RcError("账号启用了 MFA：需要人工/自动化提交 TOTP 码后才能建立会话")
        return body or {}

    # --------------------------------------------------------------- queries
    def me(self):              return self.get("/api/app/me")
    def orgs(self):            return self.get("/api/app/orgs/")
    def devices(self):         return self.get("/api/app/devices") or []
    def device_quota(self):    return self.get("/api/app/devices/quota") or {}

    def usage(self, device_id: int | None = None, org_id: int | None = None, rng: str = "all"):
        q = [f"range={rng}"]
        if device_id is not None:
            q.append(f"device_id={device_id}")
        if org_id is not None:
            q.append(f"org_id={org_id}")
        return self.get("/api/app/usage/stats?" + "&".join(q)) or {}

    def history(self, since: int | None = None, until: int | None = None,
                cursor: str | None = None, limit: int = 200,
                device_id: int | None = None, **extra):
        """请求流水。limit 上限 200；翻页只能靠 next_cursor；device_id 只接受单个数字。"""
        q = [f"limit={min(limit, 200)}"]
        if since is not None:
            q.append(f"since={int(since)}")
        if until is not None:
            q.append(f"until={int(until)}")
        if cursor:
            q.append("cursor=" + urllib.parse.quote(str(cursor)))
        if device_id is not None:
            q.append(f"device_id={int(device_id)}")
        for k, v in extra.items():
            if v is not None:
                q.append(f"{k}={urllib.parse.quote(str(v))}")
        return self.get("/api/app/history/messages?" + "&".join(q)) or {}

    # ------------------------------------------------------------ mutating
    def revoke_device(self, rc_id: int):
        return self.post(f"/api/app/devices/{rc_id}/revoke")

    def cli_describe(self, state: str):
        return self.post("/api/cli/auth/describe", {"state": state})

    def cli_approve(self, state: str, device_name: str, org_id: int):
        return self.post("/api/cli/auth/approve",
                         {"state": state, "device_name": device_name, "org_id": org_id})

    def refresh_account_usage(self, org_id: int | None = None):
        path = "/api/app/account/usage/refresh"
        if org_id is not None:
            path += f"?org_id={org_id}"
        return self.post(path)


def load_env(path: str = "/opt/data/.env") -> dict:
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def retry(fn, attempts: int = 3, delay: float = 1.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except RcError as exc:
            last = exc
            if "401" in str(exc):
                raise
            time.sleep(delay * (i + 1))
    raise last


# urllib.parse 在 _load_cookie 里用到
import urllib.parse  # noqa: E402
