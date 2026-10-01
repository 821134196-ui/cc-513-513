"""SQLite 存储：当前状态表 + 只追加事件表（审计留痕）。

带看登记在数据库层用部分唯一索引防双记录，并发写入由数据库裁决。
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    employee_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT,
    role TEXT NOT NULL DEFAULT 'agent',          -- agent | supervisor
    can_cross_store INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    effective_from TEXT NOT NULL,                -- 生效日 YYYY-MM-DD
    body_json TEXT NOT NULL,                     -- 分成与资格规则
    created_by INTEGER REFERENCES agents(id),
    published_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),  -- 房源方门店
    address TEXT NOT NULL,
    district TEXT,
    price REAL,
    status TEXT NOT NULL DEFAULT 'on',           -- on | off | dealt
    listed_ts TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    created_store_id INTEGER REFERENCES stores(id),
    created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    kind TEXT NOT NULL,                          -- phone | idno | name
    value TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
-- 仅强标识（手机号/证件号）全局唯一：命中即判定为同一客户；姓名重名允许并存。
CREATE UNIQUE INDEX IF NOT EXISTS ux_client_strong_alias
    ON client_aliases(kind, value) WHERE kind IN ('phone','idno');

CREATE TABLE IF NOT EXISTS viewings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    agent_id INTEGER REFERENCES agents(id),      -- 自行到店时可为空
    store_id INTEGER NOT NULL REFERENCES stores(id),
    appointment_ts TEXT NOT NULL,                -- 本地时间 ISO
    is_self_visit INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'scheduled',    -- scheduled|checked_in|cancelled
    checkin_ts TEXT,
    sig_agent_path TEXT,
    sig_client_path TEXT,
    client_confirmed INTEGER NOT NULL DEFAULT 0,
    client_confirmed_ts TEXT,
    cross_store INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_ts TEXT NOT NULL
);
-- 防双记录：同一客户/房源/经纪人/带看日只允许一条“有效”登记；取消后释放，允许重约。
CREATE UNIQUE INDEX IF NOT EXISTS ux_viewings_active
    ON viewings(client_id, property_id, agent_id, date(appointment_ts))
    WHERE status != 'cancelled' AND agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_viewings_prop ON viewings(property_id);
CREATE INDEX IF NOT EXISTS idx_viewings_client ON viewings(client_id);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    deal_date TEXT NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL,
    rule_version_id INTEGER REFERENCES rule_versions(id),
    protection_active INTEGER NOT NULL,
    recorded_ts TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',         -- open | decided
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    window_days INTEGER NOT NULL,
    opened_by INTEGER REFERENCES agents(id),
    opened_ts TEXT NOT NULL,
    decided_ts TEXT
);

CREATE TABLE IF NOT EXISTS dispute_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL REFERENCES disputes(id),
    agent_id INTEGER REFERENCES agents(id),      -- 留空表示门店/自行到店占位
    store_id INTEGER NOT NULL REFERENCES stores(id),
    role_label TEXT NOT NULL,                    -- first|same_day|cross_store|self_visit|home_store
    suggested_share REAL NOT NULL,
    viewing_ids TEXT NOT NULL,                   -- JSON: 证据带看 id 列表
    evidence_json TEXT NOT NULL                  -- 快照：签到/签字/客户确认
);

CREATE TABLE IF NOT EXISTS dispute_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL UNIQUE REFERENCES disputes(id),
    decided_by INTEGER NOT NULL REFERENCES agents(id),
    decided_ts TEXT NOT NULL,
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    outcome_json TEXT NOT NULL,                  -- 各候选最终份额
    note TEXT NOT NULL,
    evidence_reviewed INTEGER NOT NULL           -- 主管确认已核对签到+客户确认
);

CREATE TABLE IF NOT EXISTS sms_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    phone TEXT NOT NULL,
    template TEXT NOT NULL,
    vars_json TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'mock'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);
"""

_lock = threading.RLock()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def append_event(conn, event_type, payload, actor=None, ts=None):
    with _lock:
        cur = conn.execute(
            "INSERT INTO events(ts, event_type, actor, payload) VALUES (?,?,?,?)",
            (ts or now_iso(), event_type, str(actor) if actor is not None else None,
             json.dumps(payload, ensure_ascii=False)),
        )
        return cur.lastrowid


def load_events(conn, event_type=None):
    sql = "SELECT * FROM events"
    rows = (conn.execute(sql + " WHERE event_type=? ORDER BY id", (event_type,)).fetchall()
            if event_type else conn.execute(sql + " ORDER BY id").fetchall())
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def claim_idempotency(conn, key):
    """表单/请求幂等键。返回 True 表示本次占位成功。"""
    if not key:
        return True
    with _lock:
        try:
            conn.execute("INSERT INTO idempotency(key, ts) VALUES (?,?)", (key, now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False
