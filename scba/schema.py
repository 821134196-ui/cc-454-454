"""SQLite schema 与初始化。全程事件落库，附件仅存路径。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 人员（训练数据）
CREATE TABLE IF NOT EXISTS people (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    badge     TEXT NOT NULL UNIQUE,
    role      TEXT NOT NULL CHECK (role IN ('fighter','operator','supervisor')),
    squad     TEXT NOT NULL DEFAULT '',
    active    INTEGER NOT NULL DEFAULT 1
);

-- 背架
CREATE TABLE IF NOT EXISTS harnesses (
    id     INTEGER PRIMARY KEY,
    code   TEXT NOT NULL UNIQUE,
    model  TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired'))
);

-- 气瓶台账
CREATE TABLE IF NOT EXISTS cylinders (
    id                    INTEGER PRIMARY KEY,
    serial_no             TEXT NOT NULL UNIQUE,
    harness_id            INTEGER REFERENCES harnesses(id),
    rated_pressure        REAL NOT NULL DEFAULT 30.0,
    manufacture_date      TEXT NOT NULL,
    last_inspection_date  TEXT NOT NULL,
    inspection_due        TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','issued','returned_pending','filling',
                          'loaned','sealed','scrapped')),
    current_pressure      REAL NOT NULL DEFAULT 30.0,
    seal_reason           TEXT DEFAULT '',
    scrap_reason          TEXT DEFAULT '',
    paired_confirmed_at   TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cyl_status ON cylinders(status);
CREATE INDEX IF NOT EXISTS idx_cyl_due ON cylinders(inspection_due);

-- 训练/任务
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT '训练',
    location     TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned','ongoing','closed')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 任务领用
CREATE TABLE IF NOT EXISTS assignments (
    id              INTEGER PRIMARY KEY,
    cylinder_id     INTEGER NOT NULL REFERENCES cylinders(id),
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    person_id       INTEGER NOT NULL REFERENCES people(id),
    issued_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    issued_pressure REAL NOT NULL,
    returned_at     TEXT,
    return_pressure REAL,
    status          TEXT NOT NULL DEFAULT 'issued'
        CHECK (status IN ('issued','returned'))
);
CREATE INDEX IF NOT EXISTS idx_asg_cyl ON assignments(cylinder_id, status);

-- 归队记录：同一气瓶只允许一条未闭环归队记录（部分唯一索引）
CREATE TABLE IF NOT EXISTS returns (
    id              INTEGER PRIMARY KEY,
    cylinder_id     INTEGER NOT NULL REFERENCES cylinders(id),
    assignment_id   INTEGER REFERENCES assignments(id),
    return_pressure REAL NOT NULL,
    returned_by     INTEGER REFERENCES people(id),
    note            TEXT NOT NULL DEFAULT '',
    dup_scan_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','inspected')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ret_cyl ON returns(cylinder_id);
-- 关键去重约束：每瓶仅一条 pending 归队记录
CREATE UNIQUE INDEX IF NOT EXISTS uq_return_open
    ON returns(cylinder_id) WHERE status = 'pending';

-- 外观检查
CREATE TABLE IF NOT EXISTS inspections (
    id               INTEGER PRIMARY KEY,
    cylinder_id      INTEGER NOT NULL REFERENCES cylinders(id),
    return_id        INTEGER REFERENCES returns(id),
    appearance_ok    INTEGER NOT NULL,
    valve_ok         INTEGER NOT NULL,
    label_ok         INTEGER NOT NULL DEFAULT 1,
    findings         TEXT NOT NULL DEFAULT '',
    inspector_id     INTEGER REFERENCES people(id),
    attachment_id    INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 充装批次
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','running','interrupted','done','closed')),
    operator_id     INTEGER REFERENCES people(id),
    interrupt_reason TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at      TEXT,
    finished_at     TEXT,
    closed_at       TEXT
);

-- 批次接口位
CREATE TABLE IF NOT EXISTS batch_positions (
    id             INTEGER PRIMARY KEY,
    batch_id       INTEGER NOT NULL REFERENCES batches(id),
    position       INTEGER NOT NULL CHECK (position BETWEEN 1 AND 4),
    cylinder_id    INTEGER NOT NULL REFERENCES cylinders(id),
    fill_final     REAL,
    retest_initial REAL,
    retest_final   REAL,
    pressure_drop  REAL,
    leak_result    TEXT CHECK (leak_result IN ('pass','leak')),
    position_state TEXT NOT NULL DEFAULT 'queued'
        CHECK (position_state IN ('queued','filling','filled','leak','low_pressure','interrupted')),
    scenario       TEXT NOT NULL DEFAULT '',  -- 模拟压力台故障注入：'' / 'leak' / 'low'
    retest_due_at  TEXT,
    retested_at    TEXT,
    released       INTEGER NOT NULL DEFAULT 0,  -- 1=气瓶已脱离该批次（回库/封存/报废）
    UNIQUE (batch_id, position)
);
-- 关键排他约束：同一气瓶只能挂在一个未释放的批次位置上
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_active
    ON batch_positions(cylinder_id) WHERE released = 0;

-- 紧急借调
CREATE TABLE IF NOT EXISTS loans (
    id             INTEGER PRIMARY KEY,
    cylinder_id    INTEGER NOT NULL REFERENCES cylinders(id),
    borrower_unit  TEXT NOT NULL,
    approver_id    INTEGER REFERENCES people(id),
    pressure_out   REAL NOT NULL,
    pressure_back  REAL,
    status         TEXT NOT NULL DEFAULT 'out'
        CHECK (status IN ('out','returned')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    returned_at    TEXT
);

-- 检测附件（文件保存在本地目录，库存路径）
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY,
    cylinder_id INTEGER NOT NULL REFERENCES cylinders(id),
    kind        TEXT NOT NULL DEFAULT 'appearance',
    filename    TEXT NOT NULL,
    relpath     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 进程内任务生成的提醒（静置复测 / 检验到期）
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('retest','expiry')),
    cylinder_id INTEGER REFERENCES cylinders(id),
    batch_id    INTEGER REFERENCES batches(id),
    due_at      TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'due'
        CHECK (status IN ('due','handled','cancelled')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(kind, status);
-- 去重：同类型同瓶同一到期时刻仅一条（NULL 不参与唯一性，故不含 batch_id）
CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder
    ON reminders(kind, cylinder_id, due_at);

-- 全程事件流
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    type        TEXT NOT NULL,
    cylinder_id INTEGER,
    batch_id    INTEGER,
    actor_id    INTEGER,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ev_cyl ON events(cylinder_id);
CREATE INDEX IF NOT EXISTS idx_ev_batch ON events(batch_id);
CREATE INDEX IF NOT EXISTS idx_ev_type ON events(type);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
