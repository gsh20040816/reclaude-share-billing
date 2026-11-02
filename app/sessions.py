"""多账号会话池：每账号一把锁、cookie 每响应回写、401 自动重登。"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import rcapi

from . import crypto, store


class AccountUnavailable(Exception):
    """账号不可用（缺凭据 / 登录失败 / MFA / 被禁用）。"""


class Session:
    """在 pool.session() 上下文内使用；所有调用串行。"""

    def __init__(self, pool: "SessionPool", account_id: str, client):
        self.pool = pool
        self.account_id = account_id
        self.client = client

    def _call(self, fn):
        try:
            return fn(self.client)
        except rcapi.RcError as exc:
            if "(401)" not in str(exc):
                raise
            self.client = self.pool._relogin(self.account_id)
            return fn(self.client)

    def get(self, path: str):
        return self._call(lambda c: c.get(path))

    def post(self, path: str, body=None):
        return self._call(lambda c: c.post(path, body))

    # 语义化封装 ---------------------------------------------------------
    def devices(self):
        return self.get("/api/app/devices") or []

    def device_quota(self):
        return self.get("/api/app/devices/quota") or {}

    def usage(self, device_id=None, org_id=None, rng="all"):
        q = [f"range={rng}"]
        if device_id is not None:
            q.append(f"device_id={int(device_id)}")
        if org_id is not None:
            q.append(f"org_id={int(org_id)}")
        return self.get("/api/app/usage/stats?" + "&".join(q)) or {}

    def history(self, **kw):
        return self._call(lambda c: c.history(**kw))

    def revoke(self, rc_device_id: int):
        return self.post(f"/api/app/devices/{int(rc_device_id)}/revoke")


class SessionPool:
    def __init__(self, conn, client_factory=None, base_url: str = rcapi.BASE_DEFAULT):
        self.conn = conn
        self.base_url = base_url
        self._locks: dict[str, threading.Lock] = {}
        self._clients: dict[str, object] = {}
        self._guard = threading.Lock()
        self.client_factory = client_factory or self._default_factory

    # ------------------------------------------------------------------
    def _default_factory(self, account_row):
        return rcapi.RcClient(base_url=self.base_url, cookie=account_row["cookie"])

    def _lock(self, account_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def _client(self, account_row):
        cid = account_row["id"]
        client = self._clients.get(cid)
        if client is None:
            client = self.client_factory(account_row)
            self._clients[cid] = client
        return client

    @contextmanager
    def session(self, account_id: str):
        with self._lock(account_id):
            row = store.account_row(self.conn, account_id)
            if row is None:
                raise AccountUnavailable(f"账号 {account_id} 不存在")
            if not row["enabled"]:
                raise AccountUnavailable(f"账号 {account_id} 已停用")
            client = self._client(row)
            sess = Session(self, account_id, client)
            ok = True
            try:
                yield sess
            except BaseException:
                ok = False
                raise
            finally:
                self._save_cookie(account_id, sess.client, ok=ok)

    # ------------------------------------------------------------------
    def _save_cookie(self, account_id: str, client, ok: bool = True) -> None:
        cookie = getattr(client, "cookie", "")
        if not cookie:
            return
        if ok:
            self.conn.execute("UPDATE rc_accounts SET cookie=?, status='ok', last_error=NULL WHERE id=?",
                              (cookie, account_id))
        else:
            # 请求失败（403/429/5xx 等）时仍回写轮转后的 cookie，但不掩盖账号状态
            self.conn.execute("UPDATE rc_accounts SET cookie=? WHERE id=?", (cookie, account_id))
        self.conn.commit()

    def _relogin(self, account_id: str):
        row = store.account_row(self.conn, account_id)
        if row is None:
            raise AccountUnavailable(f"账号 {account_id} 不存在")
        password = crypto.decrypt(row["password_enc"] or "")
        if not row["email"] or not password:
            self._mark(account_id, "cookie_expired", "会话失效且没有可用的邮箱/密码")
            raise AccountUnavailable(f"账号 {account_id} 会话失效且缺少凭据")
        client = rcapi.RcClient(base_url=self.base_url, cookie=None)
        try:
            client.login(row["email"], password)
        except rcapi.RcError as exc:
            msg = str(exc)
            status = "mfa_required" if "MFA" in msg else "error"
            self._mark(account_id, status, msg[:400])
            raise AccountUnavailable(f"账号 {account_id} 重新登录失败：{msg[:160]}") from exc
        with self._guard:
            self._clients[account_id] = client
        self.conn.execute("UPDATE rc_accounts SET cookie=?, status='ok', last_login_at=?, last_error=NULL "
                          "WHERE id=?", (client.cookie, store.iso(), account_id))
        self.conn.commit()
        return client

    def _mark(self, account_id: str, status: str, error: str) -> None:
        self.conn.execute("UPDATE rc_accounts SET status=?, last_error=? WHERE id=?",
                          (status, error, account_id))
        self.conn.commit()

    # ------------------------------------------------------------------
    def login_now(self, account_id: str) -> dict:
        """强制用存储的密码重新登录一次（加账号后立即验证）。"""
        with self._lock(account_id):
            self._relogin(account_id)
            row = store.account_row(self.conn, account_id)
            client = self._clients[account_id]
        me = client.me()
        orgs = client.orgs()
        own_org = None
        for org in (orgs.get("items") or []):
            acct = org.get("current_account") or {}
            if acct.get("status") == "bound":
                own_org = org.get("id")
                break
        own_org = own_org or orgs.get("default_org_id")
        quota = client.device_quota()
        self.conn.execute(
            "UPDATE rc_accounts SET own_org_id=?, tier=?, capacity=?, status='ok', last_error=NULL WHERE id=?",
            (own_org,
             ((me.get("current_account") or {}).get("subscription_type")),
             quota.get("max"),
             account_id))
        self.conn.commit()
        return {"own_org_id": own_org, "capacity": quota.get("max"),
                "quota": quota, "email": row["email"]}
