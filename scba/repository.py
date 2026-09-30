"""只读查询层：为七个视图装配数据。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config, dates


def _cyl_select() -> str:
    return """
        SELECT c.*, h.code AS harness_code, h.model AS harness_model,
               (julianday(c.inspection_due)-julianday('now','localtime')) AS days_left_f
        FROM cylinders c LEFT JOIN harnesses h ON h.id=c.harness_id
    """


def _annotate(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["status_name"] = config.CYL_STATUS_NAMES.get(d["status"], d["status"])
    d["days_left"] = dates.days_until(d["inspection_due"])
    d["due_state"] = (
        "overdue" if d["days_left"] < 0
        else "soon" if d["days_left"] <= config.EXPIRY_WARN_DAYS
        else "ok"
    )
    d["pressure_pct"] = round(
        max(0.0, min(1.2, d["current_pressure"] / d["rated_pressure"])) * 100
    )
    return d


# --------------------------------------------------------------------------- #
# 1. 装备台账
# --------------------------------------------------------------------------- #
def ledger(conn: sqlite3.Connection, status: Optional[str] = None,
           q: Optional[str] = None) -> list[dict]:
    sql = _cyl_select() + " WHERE 1=1"
    args: list = []
    if status:
        sql += " AND c.status=?"
        args.append(status)
    if q:
        sql += " AND (c.serial_no LIKE ? OR h.code LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY c.serial_no"
    return [_annotate(r) for r in conn.execute(sql, args)]


def cylinder_detail(conn: sqlite3.Connection, cid: int) -> dict:
    r = conn.execute(_cyl_select() + " WHERE c.id=?", (cid,)).fetchone()
    if not r:
        return {}
    d = _annotate(r)
    d["inspections"] = [dict(x) for x in conn.execute(
        "SELECT i.*, p.name AS inspector_name FROM inspections i "
        "LEFT JOIN people p ON p.id=i.inspector_id WHERE i.cylinder_id=? ORDER BY i.id DESC",
        (cid,))]
    d["attachments"] = [dict(x) for x in conn.execute(
        "SELECT * FROM attachments WHERE cylinder_id=? ORDER BY id DESC", (cid,))]
    d["events"] = [dict(x) for x in conn.execute(
        "SELECT e.*, p.name AS actor_name FROM events e "
        "LEFT JOIN people p ON p.id=e.actor_id WHERE e.cylinder_id=? ORDER BY e.id DESC LIMIT 20",
        (cid,))]
    return d


# --------------------------------------------------------------------------- #
# 2. 任务领用
# --------------------------------------------------------------------------- #
def tasks_view(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for t in conn.execute("SELECT * FROM tasks ORDER BY scheduled_at"):
        td = dict(t)
        td["assignments"] = [dict(a) for a in conn.execute(
            """SELECT a.*, c.serial_no, c.status AS cyl_status, p.name AS person_name,
                      p.badge AS person_badge
               FROM assignments a
               JOIN cylinders c ON c.id=a.cylinder_id
               JOIN people p ON p.id=a.person_id
               WHERE a.task_id=? ORDER BY a.id""", (t["id"],))]
        out.append(td)
    return out


def people(conn: sqlite3.Connection, role: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM people WHERE active=1"
    args: list = []
    if role:
        sql += " AND role=?"
        args.append(role)
    sql += " ORDER BY role, id"
    return [dict(r) for r in conn.execute(sql, args)]


# --------------------------------------------------------------------------- #
# 3. 归队检查
# --------------------------------------------------------------------------- #
def returns_view(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, c.serial_no, c.rated_pressure, c.inspection_due, c.harness_id,
                  h.code AS harness_code, p.name AS returned_by_name
           FROM returns r
           JOIN cylinders c ON c.id=r.cylinder_id
           LEFT JOIN harnesses h ON h.id=c.harness_id
           LEFT JOIN people p ON p.id=r.returned_by
           ORDER BY r.status DESC, r.id DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["days_left"] = dates.days_until(r["inspection_due"])
        d["low_pressure"] = r["return_pressure"] < config.RETURN_LOW_PRESSURE
        latest = conn.execute(
            "SELECT * FROM inspections WHERE return_id=? ORDER BY id DESC LIMIT 1", (r["id"],)
        ).fetchone()
        d["latest_inspection"] = dict(latest) if latest else None
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 4. 充装组批
# --------------------------------------------------------------------------- #
def batches_view(conn: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM batches"
    if active_only:
        sql += " WHERE status IN ('open','running','done')"
    sql += " ORDER BY id DESC"
    out = []
    for b in conn.execute(sql):
        bd = dict(b)
        bd["positions"] = [dict(p) for p in conn.execute(
            """SELECT bp.*, c.serial_no, c.rated_pressure, c.current_pressure
               FROM batch_positions bp JOIN cylinders c ON c.id=bp.cylinder_id
               WHERE bp.batch_id=? ORDER BY bp.position""", (b["id"],))]
        out.append(bd)
    return out


def batch_detail(conn: sqlite3.Connection, bid: int) -> dict:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if not b:
        return {}
    bd = dict(b)
    bd["positions"] = [dict(p) for p in conn.execute(
        """SELECT bp.*, c.serial_no, c.rated_pressure, c.current_pressure
           FROM batch_positions bp JOIN cylinders c ON c.id=bp.cylinder_id
           WHERE bp.batch_id=? ORDER BY bp.position""", (bid,))]
    return bd


# --------------------------------------------------------------------------- #
# 5. 压力复测
# --------------------------------------------------------------------------- #
def retest_view(conn: sqlite3.Connection) -> dict:
    due = [dict(r) for r in conn.execute(
        """SELECT bp.id AS position_id, bp.batch_id, bp.position, bp.fill_final,
                  bp.retest_initial, bp.retest_due_at, bp.scenario, b.code AS batch_code,
                  c.serial_no
           FROM batch_positions bp JOIN cylinders c ON c.id=bp.cylinder_id
           JOIN batches b ON b.id=bp.batch_id
           WHERE bp.position_state='filled' AND bp.released=0
           ORDER BY bp.retest_due_at""")]
    done = [dict(r) for r in conn.execute(
        """SELECT bp.id AS position_id, bp.batch_id, bp.position, bp.fill_final,
                  bp.retest_initial, bp.retest_final, bp.pressure_drop, bp.leak_result,
                  bp.position_state, b.code AS batch_code, c.serial_no
           FROM batch_positions bp JOIN cylinders c ON c.id=bp.cylinder_id
           JOIN batches b ON b.id=bp.batch_id
           WHERE bp.retested_at IS NOT NULL
           ORDER BY bp.retested_at DESC LIMIT 30""")]
    return {"due": due, "done": done}


# --------------------------------------------------------------------------- #
# 6. 配套核验
# --------------------------------------------------------------------------- #
def pairing_view(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT c.id, c.serial_no, c.status, c.harness_id, c.paired_confirmed_at,
                  h.code AS harness_code, h.model AS harness_model
           FROM cylinders c LEFT JOIN harnesses h ON h.id=c.harness_id
           WHERE c.status!='scrapped' ORDER BY c.serial_no""")]


def harnesses(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM harnesses WHERE status='active' ORDER BY code")]


# --------------------------------------------------------------------------- #
# 7. 检验到期
# --------------------------------------------------------------------------- #
def expiry_view(conn: sqlite3.Connection) -> dict:
    rows = [_annotate(r) for r in conn.execute(_cyl_select() + " ORDER BY c.inspection_due")]
    return {
        "warn_days": config.EXPIRY_WARN_DAYS,
        "overdue": [c for c in rows if c["due_state"] == "overdue"],
        "soon": [c for c in rows if c["due_state"] == "soon"],
        "all": rows,
    }


def reminders(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT rm.*, c.serial_no FROM reminders rm
           LEFT JOIN cylinders c ON c.id=rm.cylinder_id
           WHERE rm.status='due' ORDER BY rm.due_at""")]


def events_feed(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT e.*, c.serial_no, b.code AS batch_code, p.name AS actor_name
           FROM events e
           LEFT JOIN cylinders c ON c.id=e.cylinder_id
           LEFT JOIN batches b ON b.id=e.batch_id
           LEFT JOIN people p ON p.id=e.actor_id
           ORDER BY e.id DESC LIMIT ?""", (limit,))]


def dashboard(conn: sqlite3.Connection) -> dict:
    def cnt(where=""):
        return conn.execute(f"SELECT COUNT(*) c FROM cylinders {where}").fetchone()["c"]
    return {
        "total": cnt(),
        "available": cnt("WHERE status='available'"),
        "pending": cnt("WHERE status='returned_pending'"),
        "filling": cnt("WHERE status='filling'"),
        "loaned": cnt("WHERE status='loaned'"),
        "sealed": cnt("WHERE status='sealed'"),
        "scrapped": cnt("WHERE status='scrapped'"),
        "open_batches": conn.execute(
            "SELECT COUNT(*) c FROM batches WHERE status IN ('open','running','done')"
        ).fetchone()["c"],
        "retest_due": conn.execute(
            "SELECT COUNT(*) c FROM batch_positions WHERE position_state='filled' AND released=0"
        ).fetchone()["c"],
        "overdue": conn.execute(
            "SELECT COUNT(*) c FROM cylinders WHERE date(inspection_due)<date('now','localtime')"
        ).fetchone()["c"],
    }
