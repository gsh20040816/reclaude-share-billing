"""运行时装配：配置、DB、会话池。"""
from __future__ import annotations

import json
import os

from . import store
from .sessions import SessionPool

CONFIG_PATH = os.environ.get("RC_BILLING_CONFIG", "/opt/data/state/reclaude-billing/config.json")

DEFAULT_CONFIG = {
    "bot_token": "",
    "admin_ids": [],
    "allowed_chat_ids": [],
    "base_url": "https://www.reclaude.ai",
    "dry_run": True,
}


def load_config(path: str | None = None) -> dict:
    path = path or CONFIG_PATH
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    if os.environ.get("RC_BOT_TOKEN"):
        cfg["bot_token"] = os.environ["RC_BOT_TOKEN"]
    return cfg


def save_config(cfg: dict, path: str | None = None) -> None:
    path = path or CONFIG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Runtime:
    def __init__(self, db_path: str | None = None, config: dict | None = None,
                 client_factory=None):
        self.cfg = config or load_config()
        self.conn = store.ThreadLocalConnection(db_path)
        store.init(self.conn)
        self.pool = SessionPool(self.conn, client_factory=client_factory,
                                base_url=self.cfg.get("base_url", "https://www.reclaude.ai"))

    def close(self):
        self.conn.close()
