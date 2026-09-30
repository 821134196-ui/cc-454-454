"""SQLite 连接、建表与全程事件追加。"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS personnel (
    person_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    unit        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    location    TEXT NOT NULL,
    planned_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OPEN'   -- OPEN / CLOSED
);

CREATE TABLE IF NOT EXISTS cylinders (
    cyl_no          TEXT PRIMARY KEY,          -- 气瓶编号
    frame_no        TEXT NOT NULL,             -- 所属背架编号
    rated_pressure  REAL NOT NULL,             -- 额定压力 MPa
    volume_l        REAL NOT NULL,             -- 容积 L
    material        TEXT NOT NULL,
    last_inspection TEXT NOT NULL,             -- 上次检测日期
    inspection_due  TEXT NOT NULL,             -- 检验有效期至
    status          TEXT NOT NULL,
    current_pressure REAL,                     -- 最近一次已知压力
    disposition     TEXT,                      -- 当前挂起处置
    created_ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (             -- 任务领用 / 紧急借调
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cyl_no        TEXT NOT NULL,
    task_id       TEXT,
    person_id     TEXT NOT NULL,
    issue_type    TEXT NOT NULL,
    target_unit   TEXT,                         -- 借调去向单位
    issue_pressure REAL,
    issued_ts     REAL NOT NULL,
    returned_ts   REAL,
    return_pressure REAL,
    scanner_id    TEXT,
    status        TEXT NOT NULL DEFAULT 'OPEN' -- OPEN / RETURNED
);

CREATE TABLE IF NOT EXISTS return_checks (      -- 归队检查（一条领用对应一条）
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id      INTEGER NOT NULL UNIQUE,
    cyl_no        TEXT NOT NULL,
    return_pressure REAL NOT NULL,
    appearance_ok INTEGER,                      -- 检查前为空
    valve_ok      INTEGER,
    within_due    INTEGER,
    result        TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/PASS/VALVE/APPEARANCE/OVERDUE
    disposition   TEXT,
    attachment_id INTEGER,
    inspector_id  TEXT,
    inspected_ts  REAL,
    created_ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fill_batches (
    id          TEXT PRIMARY KEY,               -- 批次号 PC-...
    status      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    start_ts    REAL,
    end_ts      REAL,
    abort_reason TEXT
);

CREATE TABLE IF NOT EXISTS batch_ports (        -- 充装接口（工位）
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL,
    position        INTEGER NOT NULL,           -- 接口位置 1..N
    cyl_no          TEXT,
    status          TEXT NOT NULL DEFAULT 'WAITING',
    final_pressure  REAL,                       -- 充装终压
    retest_initial  REAL,                       -- 静置初值
    retest_final    REAL,                       -- 静置复测终值
    retest_drop     REAL,
    settled_due     REAL,
    retested_ts     REAL,
    retest_by       TEXT,
    UNIQUE(batch_id, position)
);

-- 排他锁：同一气瓶在任意“未结束批次”中至多一条（充装中断/完成即释放）
CREATE TABLE IF NOT EXISTS active_batch_cyl (
    cyl_no    TEXT PRIMARY KEY,
    batch_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pair_checks (        -- 配套核验
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cyl_no        TEXT NOT NULL,
    frame_scanned TEXT NOT NULL,
    frame_expected TEXT NOT NULL,
    matched       INTEGER NOT NULL,
    actor_id      TEXT NOT NULL,
    ts            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seal_records (       -- 封存/解封/报废审议
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cyl_no     TEXT NOT NULL,
    action     TEXT NOT NULL,                   -- SEAL/UNSEAL/CONDEMN_PROPOSE/CONDEMN_CONFIRM
    reason     TEXT,
    actor_id   TEXT NOT NULL,
    ts         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (        -- 检测附件（本地目录留存）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cyl_no      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,                    -- SETTLE_DUE/INSPECTION_DUE/INSPECTION_OVERDUE
    ref_id    TEXT NOT NULL,                    -- 去重键（气瓶号或接口）
    cyl_no    TEXT NOT NULL,
    message   TEXT NOT NULL,
    due_ts    REAL,
    ts        REAL NOT NULL,
    ack       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(type, ref_id)
);

-- 全程事件流（只追加）
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    type      TEXT NOT NULL,
    cyl_no    TEXT,
    actor_id  TEXT,
    ref_type  TEXT,
    ref_id    TEXT,
    payload   TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_cyl ON events(cyl_no, id);
CREATE INDEX IF NOT EXISTS ix_ports_cyl ON batch_ports(cyl_no);
CREATE INDEX IF NOT EXISTS ix_issues_status ON issues(status);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    # check_same_thread=False：ThreadingHTTPServer 跨线程共享，
    # 调用方以 AppState.lock（RLock）串行化所有读写。
    conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def append_event(conn, etype, cyl_no=None, actor=None,
                 ref_type=None, ref_id=None, payload=None):
    """所有状态流转统一追加事件。"""
    conn.execute(
        """INSERT INTO events(ts,type,cyl_no,actor_id,ref_type,ref_id,payload)
           VALUES(?,?,?,?,?,?,?)""",
        (time.time(), etype, cyl_no, actor, ref_type,
         str(ref_id) if ref_id is not None else None,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None),
    )
