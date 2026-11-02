"""账号密码的静态加密（Fernet）。密钥文件 0600，与库分离。"""
from __future__ import annotations

import os
import stat

from cryptography.fernet import Fernet, InvalidToken

KEY_PATH = os.environ.get("RC_BILLING_KEY", "/opt/data/state/reclaude-billing/secret.key")
PREFIX = "fernet:"


def _key() -> bytes:
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return PREFIX + Fernet(_key()).encrypt(plain.encode()).decode()


def decrypt(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored                      # 兼容手工写入的明文（不建议）
    try:
        return Fernet(_key()).decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("密码解密失败：secret.key 与库不匹配") from exc


def key_perms_ok() -> bool:
    try:
        return stat.S_IMODE(os.stat(KEY_PATH).st_mode) == 0o600
    except FileNotFoundError:
        return True
