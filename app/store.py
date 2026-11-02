"""SQLite 存储层：schema、连接、设置项。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import threading

DB_PATH = os.environ.get("RC_BILLING_DB", "/opt/data/state/reclaude-billing/billing.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  tg_user_id INTEGER PRIMARY KEY,
  handle     TEXT,
  role       TEXT NOT NULL DEFAULT 'user',
  status     TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rc_accounts (
  id            TEXT PRIMARY KEY,
  label         TEXT,
  email         TEXT NOT NULL,
  password_enc  TEXT,
  cookie        TEXT,
  status        TEXT NOT NULL DEFAULT 'ok',
  own_org_id    INTEGER,
  tier          TEXT,
  capacity      INTEGER,
  last_login_at TEXT,
  last_error    TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  account_id     TEXT NOT NULL,
  rc_device_id   INTEGER NOT NULL,
  user_id        INTEGER,
  name           TEXT,
  sk_prefix      TEXT,
  org_id         INTEGER,
  platform       TEXT,
  client_version TEXT,
  first_seen_at  TEXT,
  last_used_at   INTEGER,
  state          TEXT NOT NULL DEFAULT 'active',
  pinned         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, rc_device_id)
);
CREATE INDEX IF NOT EXISTS idx_dev_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_dev_rc ON devices(rc_device_id);

CREATE TABLE IF NOT EXISTS requests (
  id             INTEGER PRIMARY KEY,
  account_id     TEXT NOT NULL,
  rc_device_id   INTEGER NOT NULL,
  user_id        INTEGER,
  org_id         INTEGER,
  created_at     INTEGER NOT NULL,
  model          TEXT,
  request_kind   TEXT,
  status         INTEGER,
  is_stream      INTEGER,
  duration_ms    INTEGER,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  cache_create_5m_tokens INTEGER,
  cache_create_1h_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  total_tokens   INTEGER,
  cost_usd       REAL,
  pricing_status TEXT,
  deduction_status TEXT,
  tracking_id    TEXT,
  first_seen_at  TEXT NOT NULL,
  priced_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_user_time ON requests(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_req_acct_time ON requests(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_req_device ON requests(account_id, rc_device_id);
CREATE INDEX IF NOT EXISTS idx_req_pricing ON requests(pricing_status, created_at);

CREATE TABLE IF NOT EXISTS watermarks (
  account_id      TEXT PRIMARY KEY,
  last_synced_at  INTEGER,
  last_request_id INTEGER,
  updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS sync_progress (
  account_id TEXT PRIMARY KEY,
  since_ms INTEGER NOT NULL,
  until_ms INTEGER NOT NULL,
  cursor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_states (
  state       TEXT PRIMARY KEY,
  tg_user_id  INTEGER NOT NULL,
  account_id  TEXT,
  status      TEXT NOT NULL,
  hostname    TEXT,
  os          TEXT,
  arch        TEXT,
  client_ip   TEXT,
  expires_at  INTEGER,
  created_at  TEXT NOT NULL,
  note        TEXT
);

CREATE TABLE IF NOT EXISTS model_rates (
  pattern    TEXT PRIMARY KEY,
  multiplier REAL NOT NULL DEFAULT 1.0,
  note       TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_periods (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  start_date  TEXT NOT NULL,
  end_date    TEXT NOT NULL,
  cost_cny    REAL NOT NULL,
  idle_split  TEXT NOT NULL DEFAULT 'none',
  note        TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE(account_id, start_date)
);

CREATE TABLE IF NOT EXISTS invoices (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  total_cny    REAL NOT NULL,
  status       TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  note         TEXT
);

CREATE TABLE IF NOT EXISTS invoice_lines (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id      INTEGER NOT NULL,
  account_id      TEXT NOT NULL,
  weighted_usd    REAL NOT NULL,
  raw_usd         REAL NOT NULL,
  requests        INTEGER NOT NULL,
  unit_price_cny  REAL NOT NULL,
  amount_cny      REAL NOT NULL,
  note            TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  actor        TEXT,
  kind         TEXT NOT NULL,
  account_id   TEXT,
  rc_device_id INTEGER,
  user_id      INTEGER,
  detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
"""

DEFAULTS = {
    "min_idle_minutes": "30",
    "sync_interval_seconds": "300",
    "sync_overlap_minutes": "10",
    "backfill_max_hours": "24",
    "price_backfill_hours": "24",
    "stale_hours": "24",
    "default_multiplier": "1.0",
    "history_page_limit": "200",
    "alert_after_failures": "3",
}


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))
    conn.commit()


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None = None) -> str:
    t = (ms if ms is not None else now_ms()) / 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def get_setting(conn, key: str, default=None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def get_int(conn, key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(conn, key, default)))
    except (TypeError, ValueError):
        return default


def get_float(conn, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(conn, key, default))
    except (TypeError, ValueError):
        return default


def event(conn, kind: str, *, actor=None, account_id=None, rc_device_id=None,
          user_id=None, detail=None) -> None:
    conn.execute(
        "INSERT INTO events(at, actor, kind, account_id, rc_device_id, user_id, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (iso(), actor, kind, account_id, rc_device_id, user_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )
    conn.commit()


def account_row(conn, account_id: str):
    return conn.execute("SELECT * FROM rc_accounts WHERE id=?", (account_id,)).fetchone()


def enabled_accounts(conn):
    return conn.execute("SELECT * FROM rc_accounts WHERE enabled=1 ORDER BY id").fetchall()


class ThreadLocalConnection:
    """One SQLite transaction owner per thread; never share commit/rollback state."""

    def __init__(self, path=None):
        self.path = path or DB_PATH
        self._local = threading.local()

    def current(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
        return conn

    def __getattr__(self, name):
        return getattr(self.current(), name)

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            del self._local.conn
