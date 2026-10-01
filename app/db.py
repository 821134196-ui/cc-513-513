"""SQLite 连接与表结构。

events 为唯一事实来源（事件库），其余表为可重建的投影。
所有命令在一个 BEGIN IMMEDIATE 事务内完成「校验 + 追加事件 + 更新投影」，
配合 ux_viewing_slot 部分唯一索引，保证同一带看时段并发登记不会产生双记录。
"""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  stream_type  TEXT NOT NULL,
  stream_id    TEXT NOT NULL,
  type         TEXT NOT NULL,
  actor        TEXT,
  rule_version TEXT,
  data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_stream ON events(stream_type, stream_id);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type);

CREATE TABLE IF NOT EXISTS idempotency (
  ide_key  TEXT PRIMARY KEY,
  result   TEXT NOT NULL,
  ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  store_id TEXT PRIMARY KEY,
  name     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  phone    TEXT,
  store_id TEXT NOT NULL,
  role     TEXT NOT NULL DEFAULT 'agent'   -- agent | supervisor
);
CREATE TABLE IF NOT EXISTS rule_versions (
  version      TEXT PRIMARY KEY,
  body         TEXT NOT NULL,
  published_at TEXT NOT NULL,
  published_by TEXT,
  is_active    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS properties (
  property_id TEXT PRIMARY KEY,
  address     TEXT NOT NULL,
  layout      TEXT,
  area_sqm    REAL,
  price_cents INTEGER NOT NULL,
  store_id    TEXT NOT NULL,              -- 房源方/钥匙方门店
  status      TEXT NOT NULL DEFAULT '在售',
  listed_at   TEXT
);
CREATE TABLE IF NOT EXISTS customers (
  customer_id          TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  phone                TEXT NOT NULL UNIQUE,
  source_agent_id      TEXT,              -- 客户来源录入人
  first_registered_at  TEXT,
  note                 TEXT,
  merged_into          TEXT
);
CREATE TABLE IF NOT EXISTS viewings (
  viewing_id       TEXT PRIMARY KEY,
  property_id      TEXT NOT NULL,
  customer_id      TEXT NOT NULL,
  agent_id         TEXT NOT NULL,         -- 人类经纪人；到店登记为 'WALKIN'
  store_id         TEXT NOT NULL,         -- 带看执行门店（WALKIN 时为接待门店）
  appointment_time TEXT NOT NULL,
  status           TEXT NOT NULL,         -- scheduled | checked | cancelled
  collab           INTEGER NOT NULL DEFAULT 0,
  rule_version     TEXT,
  created_at       TEXT,
  checked_in_at    TEXT,
  lat              REAL,
  lng              REAL,
  sign_path        TEXT,
  confirm_code     TEXT,
  confirmed_at     TEXT,
  sms_sent         INTEGER NOT NULL DEFAULT 0,
  cancelled_at     TEXT,
  cancel_reason    TEXT
);
-- 同一客户/房源/时刻只允许一条有效带看（取消后释放槽位）
CREATE UNIQUE INDEX IF NOT EXISTS ux_viewing_slot
  ON viewings(property_id, customer_id, appointment_time)
  WHERE status <> 'cancelled';

CREATE TABLE IF NOT EXISTS collab_grants (
  grant_id    TEXT PRIMARY KEY,
  property_id TEXT NOT NULL,
  agent_id    TEXT NOT NULL,           -- 被授权的外店经纪人
  granted_by  TEXT NOT NULL,           -- 房源方门店主管
  granted_at  TEXT NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deals (
  deal_id      TEXT PRIMARY KEY,
  property_id  TEXT NOT NULL,
  customer_id  TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  deal_date    TEXT NOT NULL,
  status       TEXT NOT NULL,          -- 系统建议 | 待裁决 | 保护期外 | 已裁决
  rule_version TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  deal_id      TEXT NOT NULL,
  agent_id     TEXT,                   -- 门店公盘时为 NULL
  store_id     TEXT,
  kind         TEXT NOT NULL,          -- agent | store_pool
  share_pct    REAL NOT NULL,
  basis        TEXT NOT NULL,
  rank_no      INTEGER NOT NULL,
  viewing_ids  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS rulings (
  ruling_id       TEXT PRIMARY KEY,
  deal_id         TEXT NOT NULL,
  decided_by      TEXT NOT NULL,
  decided_at      TEXT NOT NULL,
  winner_agent_id TEXT,
  winner_store_id TEXT,
  split_json      TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  rule_version    TEXT NOT NULL,
  evidence_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_outbox (
  sms_id       TEXT PRIMARY KEY,
  phone        TEXT NOT NULL,
  content      TEXT NOT NULL,
  sent_at      TEXT NOT NULL,
  related_type TEXT,
  related_id   TEXT
);

-- 被拒绝动作的审计（如未授权跨店登记、重复客户尝试）
CREATE TABLE IF NOT EXISTS denied_actions (
  denial_id TEXT PRIMARY KEY,
  ts        TEXT NOT NULL,
  actor     TEXT,
  action    TEXT NOT NULL,
  reason    TEXT NOT NULL,
  detail    TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class Database:
    """线程局部连接池：每个线程一条连接；WAL 允许并发读，
    BEGIN IMMEDIATE 保证写事务全局串行，配合唯一索引杜绝双记录。"""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            init_db(c)
            self._local.conn = c
        return c
