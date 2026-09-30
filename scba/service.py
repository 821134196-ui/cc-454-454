"""领域服务：SCBA 气瓶归队 → 检查 → 组批 → 充装 → 静置复测 → 回库/配套 全流程。

所有写操作在单个 SQLite 连接/事务内完成，并写入 events 全程事件流。
规则要点：
- 归队即“待检查”；多人重复扫描同瓶只产生一条归队记录（dup_scan_count 累加）。
- 外观合格（含瓶阀）且检验在有效期内，才能加入充装批次。
- 同一气瓶不能进入两个未结束批次（DB 部分唯一索引兜底）。
- 终压不达标、复测泄漏分别处置；压力稳定方可回到可领用。
- 重新配套必须核对编号；封存/报废瓶不可领用、不可组批。
- 解除封存、报废确认仅装备主管可操作。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from . import config, dates
from .adapter import PressureBenchAdapter
from .errors import Conflict, NotFound, PermissionDenied, SCBAError

ROLE = config.ROLE_SUPERVISOR


# --------------------------------------------------------------------------- #
# 基础辅助
# --------------------------------------------------------------------------- #
def _row(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, args).fetchone()


def _require_role(actor: sqlite3.Row, role: str) -> None:
    if not actor or actor["role"] != role:
        raise PermissionDenied(
            f"该操作仅允许{config.ROLE_NAMES[role]}执行", code="permission_denied"
        )


def _get(conn: sqlite3.Connection, table: str, pk: int) -> sqlite3.Row:
    row = _row(conn, f"SELECT * FROM {table} WHERE id=?", (pk,))
    if not row:
        raise NotFound(f"{table}#{pk} 不存在")
    return row


def log_event(
    conn: sqlite3.Connection,
    etype: str,
    *,
    cylinder_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    actor_id: Optional[int] = None,
    detail: Any = "",
) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO events(type,cylinder_id,batch_id,actor_id,detail) VALUES(?,?,?,?,?)",
        (etype, cylinder_id, batch_id, actor_id, detail),
    )


def _person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    p = _row(conn, "SELECT * FROM people WHERE id=?", (person_id,))
    if not p:
        raise NotFound(f"人员#{person_id} 不存在")
    return p


# --------------------------------------------------------------------------- #
# 任务领用
# --------------------------------------------------------------------------- #
def issue_cylinder(
    conn: sqlite3.Connection, cylinder_id: int, task_id: int, person_id: int
) -> dict:
    """战斗员领用：封存/报废瓶不可领用。"""
    cyl = _get(conn, "cylinders", cylinder_id)
    task = _get(conn, "tasks", task_id)
    person = _person(conn, person_id)
    if cyl["status"] in (config.CYL_SEALED, config.CYL_SCRAPPED):
        raise SCBAError(
            f"气瓶 {cyl['serial_no']} 状态为"
            f"{config.CYL_STATUS_NAMES[cyl['status']]}，不可领用",
            code="not_issueable",
        )
    if cyl["status"] != config.CYL_AVAILABLE:
        raise Conflict(
            f"气瓶 {cyl['serial_no']} 当前{config.CYL_STATUS_NAMES[cyl['status']]}，不可领用"
        )
    if task["status"] == "closed":
        raise SCBAError("任务已结束，不能再领用")

    cur = conn.execute(
        """INSERT INTO assignments(cylinder_id,task_id,person_id,issued_pressure)
           VALUES(?,?,?,?)""",
        (cylinder_id, task_id, person_id, cyl["current_pressure"]),
    )
    conn.execute("UPDATE cylinders SET status=? WHERE id=?",
                 (config.CYL_ISSUED, cylinder_id))
    log_event(
        conn, "issue", cylinder_id=cylinder_id, actor_id=person_id,
        detail={"task": task["code"], "pressure": cyl["current_pressure"]},
    )
    conn.commit()
    return {"assignment_id": cur.lastrowid}


def return_cylinder(
    conn: sqlite3.Connection,
    cylinder_id: int,
    return_pressure: float,
    person_id: int,
    *,
    note: str = "",
) -> dict:
    """归队登记。多人重复扫描同一未闭环瓶：只保留一条归队记录并累加扫描次数。"""
    cyl = _get(conn, "cylinders", cylinder_id)
    person = _person(conn, person_id)
    if cyl["status"] == config.CYL_SCRAPPED:
        raise SCBAError("报废气瓶不能归队登记")

    # 找到该瓶未归还的领用记录
    asg = _row(
        conn,
        """SELECT * FROM assignments WHERE cylinder_id=? AND status='issued'
           ORDER BY id DESC LIMIT 1""",
        (cylinder_id,),
    )

    # 幂等去重：已存在 pending 归队记录 -> 重复扫描
    existing = _row(
        conn, "SELECT * FROM returns WHERE cylinder_id=? AND status='pending'", (cylinder_id,)
    )
    if existing:
        conn.execute(
            "UPDATE returns SET dup_scan_count=dup_scan_count+1 WHERE id=?", (existing["id"],)
        )
        log_event(
            conn, "return_rescan", cylinder_id=cylinder_id, actor_id=person_id,
            detail={"return_id": existing["id"], "dup": existing["dup_scan_count"] + 1},
        )
        conn.commit()
        return {"return_id": existing["id"], "dedup": True,
                "dup_scan_count": existing["dup_scan_count"] + 1}

    if return_pressure < 0:
        raise SCBAError("归队余压不能为负")
    low = return_pressure < config.RETURN_LOW_PRESSURE
    cur = conn.execute(
        """INSERT INTO returns(cylinder_id,assignment_id,return_pressure,returned_by,note)
           VALUES(?,?,?,?,?)""",
        (cylinder_id, asg["id"] if asg else None, return_pressure, person_id, note),
    )
    if asg:
        conn.execute(
            "UPDATE assignments SET status='returned',returned_at=datetime('now','localtime'),"
            "return_pressure=? WHERE id=?",
            (return_pressure, asg["id"]),
        )
    conn.execute(
        "UPDATE cylinders SET status=?, current_pressure=? WHERE id=?",
        (config.CYL_RETURNED, return_pressure, cylinder_id),
    )
    log_event(
        conn, "return", cylinder_id=cylinder_id, actor_id=person_id,
        detail={"pressure": return_pressure, "low": low, "note": note},
    )
    conn.commit()
    return {"return_id": cur.lastrowid, "dedup": False, "low_pressure": low}


# --------------------------------------------------------------------------- #
# 外观检查 / 入批门槛
# --------------------------------------------------------------------------- #
def inspect_appearance(
    conn: sqlite3.Connection,
    return_id: int,
    *,
    appearance_ok: bool,
    valve_ok: bool,
    label_ok: bool = True,
    findings: str = "",
    inspector_id: int,
    attachment_id: Optional[int] = None,
) -> dict:
    """外观检查。瓶阀损伤 -> 封存；超期未检 -> 封存；合格 -> 等待组批。"""
    rec = _get(conn, "returns", return_id)
    cyl = _get(conn, "cylinders", rec["cylinder_id"])
    inspector = _person(conn, inspector_id)

    days_left = dates.days_until(cyl["inspection_due"])
    overdue = days_left < 0

    conn.execute(
        """INSERT INTO inspections(cylinder_id,return_id,appearance_ok,valve_ok,label_ok,
               findings,inspector_id,attachment_id) VALUES(?,?,?,?,?,?,?,?)""",
        (cyl["id"], return_id, int(appearance_ok), int(valve_ok), int(label_ok),
         findings, inspector_id, attachment_id),
    )

    result = "pass"
    if not valve_ok:
        result = "valve_damage"
        _seal(conn, cyl, "瓶阀损伤：外观检查判定瓶阀损伤，封存待修", inspector_id,
              event="valve_damage")
    elif not appearance_ok or not label_ok:
        result = "appearance_defect"
        _seal(conn, cyl, f"外观/标识缺陷：{findings or '外观检查不合格'}", inspector_id,
              event="appearance_defect")
    elif overdue:
        result = "overdue"
        _seal(conn, cyl,
              f"超期未检：检验有效期至 {cyl['inspection_due']}，已超期 {-days_left} 天",
              inspector_id, event="overdue")
    else:
        # 合格：关闭归队记录，气瓶进入“可组批”池（保留待检查态直至入批）
        conn.execute("UPDATE returns SET status='inspected' WHERE id=?", (return_id,))
        log_event(conn, "inspection_pass", cylinder_id=cyl["id"], actor_id=inspector_id,
                  detail={"days_left": days_left})

    conn.commit()
    return {"result": result, "days_left": days_left}


def _seal(conn, cyl, reason: str, actor_id: int, *, event: str = "seal") -> None:
    conn.execute(
        "UPDATE cylinders SET status=?, seal_reason=? WHERE id=?",
        (config.CYL_SEALED, reason, cyl["id"]),
    )
    conn.execute("UPDATE returns SET status='inspected' WHERE cylinder_id=? AND status='pending'",
                 (cyl["id"],))
    # 释放该瓶仍占用的批次接口位（保留终压不足/泄漏等已定性的接口状态）
    conn.execute(
        """UPDATE batch_positions SET released=1,
           position_state=CASE WHEN position_state IN ('leak','low_pressure')
                          THEN position_state ELSE 'interrupted' END
           WHERE cylinder_id=? AND released=0""",
        (cyl["id"],),
    )
    log_event(conn, event, cylinder_id=cyl["id"], actor_id=actor_id, detail=reason)


# --------------------------------------------------------------------------- #
# 充装组批
# --------------------------------------------------------------------------- #
def create_batch(conn: sqlite3.Connection, operator_id: int) -> dict:
    op = _person(conn, operator_id)
    import secrets
    code = f"PB-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
    cur = conn.execute(
        "INSERT INTO batches(code,operator_id,status) VALUES(?,?, 'open')", (code, operator_id)
    )
    log_event(conn, "batch_create", batch_id=cur.lastrowid, actor_id=operator_id, detail=code)
    conn.commit()
    return {"batch_id": cur.lastrowid, "code": code}


def add_to_batch(
    conn: sqlite3.Connection, batch_id: int, cylinder_id: int,
    *, position: Optional[int] = None, scenario: str = "",
) -> dict:
    """把合格气瓶加入开放批次的指定接口位。执行批次排他与状态校验。"""
    batch = _get(conn, "batches", batch_id)
    cyl = _get(conn, "cylinders", cylinder_id)
    if batch["status"] not in (config.BATCH_OPEN, config.BATCH_RUNNING):
        raise SCBAError(f"批次 {batch['code']} 已结束，不能加入气瓶")
    if cyl["status"] in (config.CYL_SEALED, config.CYL_SCRAPPED):
        raise SCBAError(
            f"{config.CYL_STATUS_NAMES[cyl['status']]}气瓶不能加入充装批次",
            code="not_issueable",
        )
    if cyl["status"] != config.CYL_RETURNED:
        raise Conflict(
            f"气瓶当前为{config.CYL_STATUS_NAMES[cyl['status']]}，须归队并通过外观检查后才能入批"
        )
    # 必须有最近一次合格外观检查（含瓶阀、标识）且在有效期
    insp = _row(
        conn,
        "SELECT * FROM inspections WHERE cylinder_id=? ORDER BY id DESC LIMIT 1",
        (cylinder_id,),
    )
    if not insp or not (insp["appearance_ok"] and insp["valve_ok"] and insp["label_ok"]):
        raise Conflict("气瓶最近一次外观检查未通过，不能入批")
    if dates.days_until(cyl["inspection_due"]) < 0:
        raise Conflict("气瓶检验已超期，不能入批")
    # 排他：同一气瓶不能进入两个未结束批次（索引 uq_position_active 兜底）
    busy = _row(
        conn,
        """SELECT bp.id, b.code FROM batch_positions bp JOIN batches b ON b.id=bp.batch_id
           WHERE bp.cylinder_id=? AND bp.released=0""",
        (cylinder_id,),
    )
    if busy:
        raise Conflict(f"气瓶已在未结束批次 {busy['code']} 中，不能重复组批")

    if position is None:
        used = {
            r["position"]
            for r in conn.execute(
                "SELECT position FROM batch_positions WHERE batch_id=? AND released=0", (batch_id,)
            )
        }
        free = [p for p in range(1, config.FILL_STATION_PORTS + 1) if p not in used]
        if not free:
            raise Conflict("该批次接口位已满")
        position = free[0]
    else:
        if not (1 <= position <= config.FILL_STATION_PORTS):
            raise SCBAError("接口位超出范围")
        occ = _row(
            conn,
            "SELECT id FROM batch_positions WHERE batch_id=? AND position=? AND released=0",
            (batch_id, position),
        )
        if occ:
            raise Conflict(f"接口位 {position} 已被占用")

    try:
        cur = conn.execute(
            """INSERT INTO batch_positions(batch_id,position,cylinder_id,scenario)
               VALUES(?,?,?,?)""",
            (batch_id, position, cylinder_id, scenario),
        )
    except sqlite3.IntegrityError as e:
        raise Conflict(f"组批失败（排他约束）：{e}")
    conn.execute(
        "UPDATE cylinders SET status=? WHERE id=?", (config.CYL_FILLING, cylinder_id)
    )
    log_event(conn, "batch_enqueue", cylinder_id=cylinder_id, batch_id=batch_id,
              detail={"position": position, "scenario": scenario})
    conn.commit()
    return {"position_id": cur.lastrowid, "position": position}


def start_fill(conn: sqlite3.Connection, batch_id: int, operator_id: int) -> dict:
    batch = _get(conn, "batches", batch_id)
    positions = conn.execute(
        "SELECT * FROM batch_positions WHERE batch_id=? AND released=0", (batch_id,)
    ).fetchall()
    if not positions:
        raise Conflict("空批次不能开始充装")
    conn.execute(
        "UPDATE batches SET status='running', started_at=datetime('now','localtime') WHERE id=?",
        (batch_id,),
    )
    for p in positions:
        conn.execute(
            "UPDATE batch_positions SET position_state='filling' WHERE id=?", (p["id"],)
        )
    log_event(conn, "fill_start", batch_id=batch_id, actor_id=operator_id,
              detail={"positions": len(positions)})
    conn.commit()
    return {"started": len(positions)}


def _adapter_for(position: sqlite3.Row) -> PressureBenchAdapter:
    return PressureBenchAdapter.for_scenario(position["scenario"] or None)


def complete_fill(conn: sqlite3.Connection, batch_id: int, operator_id: int) -> dict:
    """充装完成：读取每个接口终压；终压不足的瓶转封存，合格瓶安排静置复测。"""
    batch = _get(conn, "batches", batch_id)
    if batch["status"] != config.BATCH_RUNNING:
        raise Conflict("批次未在充装中，不能完成")
    positions = conn.execute(
        "SELECT * FROM batch_positions WHERE batch_id=? AND released=0", (batch_id,)
    ).fetchall()

    low_serials, ready = [], []
    for p in positions:
        cyl = _get(conn, "cylinders", p["cylinder_id"])
        bench = _adapter_for(p)
        final = bench.final_reading(cyl["current_pressure"])
        initial = bench.initial_reading(cyl["current_pressure"])
        conn.execute(
            "UPDATE cylinders SET current_pressure=? WHERE id=?", (final, cyl["id"])
        )
        if final < config.FILL_FINAL_MIN:
            conn.execute(
                """UPDATE batch_positions SET fill_final=?,retest_initial=?,
                   position_state='low_pressure' WHERE id=?""",
                (final, initial, p["id"]),
            )
            _seal(conn, cyl,
                  f"终压不足：充装终压 {final} MPa 低于 {config.FILL_FINAL_MIN} MPa",
                  operator_id, event="low_final")
            low_serials.append(cyl["serial_no"])
        else:
            due = dates.settle_due_text(config.RETEST_SETTLE_SECONDS)
            conn.execute(
                """UPDATE batch_positions SET fill_final=?,retest_initial=?,
                   position_state='filled', retest_due_at=? WHERE id=?""",
                (final, initial, due, p["id"]),
            )
            conn.execute(
                """INSERT INTO reminders(kind,cylinder_id,batch_id,due_at,message)
                   VALUES('retest',?,?,?,?)""",
                (cyl["id"], batch_id, due, f"{cyl['serial_no']} 静置复测到期"),
            )
            ready.append(cyl["serial_no"])
    conn.execute(
        "UPDATE batches SET status='done', finished_at=datetime('now','localtime') WHERE id=?",
        (batch_id,),
    )
    log_event(conn, "fill_complete", batch_id=batch_id, actor_id=operator_id,
              detail={"low": low_serials, "awaiting_retest": ready})
    conn.commit()
    return {"low_final": low_serials, "awaiting_retest": ready}


def interrupt_batch(conn: sqlite3.Connection, batch_id: int, reason: str,
                    operator_id: int) -> dict:
    """充装中断：在充瓶全部退出，恢复待检查，可重新组批。"""
    batch = _get(conn, "batches", batch_id)
    if batch["status"] not in (config.BATCH_OPEN, config.BATCH_RUNNING):
        raise Conflict("仅开放/充装中的批次可以中断")
    positions = conn.execute(
        "SELECT * FROM batch_positions WHERE batch_id=? AND released=0", (batch_id,)
    ).fetchall()
    for p in positions:
        conn.execute(
            "UPDATE batch_positions SET released=1, position_state='interrupted' WHERE id=?",
            (p["id"],),
        )
        conn.execute(
            "UPDATE cylinders SET status=? WHERE id=?",
            (config.CYL_RETURNED, p["cylinder_id"]),
        )
        conn.execute(
            "UPDATE reminders SET status='cancelled' WHERE cylinder_id=? AND kind='retest' "
            "AND status='due'", (p["cylinder_id"],)
        )
    conn.execute(
        "UPDATE batches SET status='interrupted', interrupt_reason=? WHERE id=?",
        (reason, batch_id),
    )
    log_event(conn, "fill_interrupt", batch_id=batch_id, actor_id=operator_id, detail=reason)
    conn.commit()
    return {"released": len(positions)}


# --------------------------------------------------------------------------- #
# 静置复测（可由后台提醒触发，也可手动提前）
# --------------------------------------------------------------------------- #
def run_retest(conn: sqlite3.Connection, position_id: int, operator_id: int,
               *, elapsed_s: Optional[float] = None) -> dict:
    p = _get(conn, "batch_positions", position_id)
    cyl = _get(conn, "cylinders", p["cylinder_id"])
    if p["released"]:
        raise Conflict("该接口位已释放")
    if p["position_state"] != "filled":
        raise Conflict("仅充装合格、等待复测的接口位可以复测")
    # 模拟压力台：复测读数对应“静置期满”。引擎在静置时长到期时触发；手动复测
    # 同样读取静置期满结果（演示/训练即时可见），故障瓶据此暴露泄漏。
    elapsed = float(elapsed_s) if elapsed_s is not None else config.RETEST_SETTLE_SECONDS
    bench = _adapter_for(p)
    final_p = bench.settle_reading(p["fill_final"], elapsed)
    # 正常瓶噪声可能使终值略高，压降按 0 计（不显示负值）
    drop = round(max(0.0, p["fill_final"] - final_p), 2)
    leak = drop > config.RETEST_DROP_LIMIT

    conn.execute(
        """UPDATE batch_positions SET retest_final=?,pressure_drop=?,leak_result=?,
           retested_at=datetime('now','localtime') WHERE id=?""",
        (final_p, drop, "leak" if leak else "pass", position_id),
    )
    if leak:
        conn.execute("UPDATE batch_positions SET position_state='leak' WHERE id=?", (position_id,))
        _seal(conn, cyl,
              f"复测泄漏：初压 {p['fill_final']} → 终压 {final_p} MPa，压降 {drop} "
              f"超过 {config.RETEST_DROP_LIMIT} MPa",
              operator_id, event="retest_leak")
        conn.execute(
            "UPDATE reminders SET status='cancelled' WHERE cylinder_id=? AND kind='retest' "
            "AND status='due'", (cyl["id"],)
        )
    else:
        # 压力稳定：回到可领用
        conn.execute("UPDATE cylinders SET status=?, current_pressure=? WHERE id=?",
                     (config.CYL_AVAILABLE, final_p, cyl["id"]))
        conn.execute("UPDATE batch_positions SET released=1 WHERE id=?", (position_id,))
        conn.execute(
            "UPDATE reminders SET status='handled' WHERE cylinder_id=? AND kind='retest' "
            "AND status='due'", (cyl["id"],)
        )
        log_event(conn, "retest_pass", cylinder_id=cyl["id"], batch_id=p["batch_id"],
                  actor_id=operator_id,
                  detail={"initial": p["fill_final"], "final": final_p, "drop": drop})
    _maybe_close_batch(conn, p["batch_id"])
    log_event(conn, "retest", cylinder_id=cyl["id"], batch_id=p["batch_id"],
              actor_id=operator_id, detail={"final": final_p, "drop": drop, "leak": leak})
    conn.commit()
    return {"final": final_p, "drop": drop, "leak": leak}


def _maybe_close_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """批次内所有接口位均已释放（回库/封存）后自动结束。"""
    active = conn.execute(
        "SELECT COUNT(*) c FROM batch_positions WHERE batch_id=? AND released=0", (batch_id,)
    ).fetchone()["c"]
    if active == 0:
        conn.execute(
            "UPDATE batches SET status='closed', closed_at=datetime('now','localtime') WHERE id=?",
            (batch_id,),
        )
        log_event(conn, "batch_close", batch_id=batch_id)


# --------------------------------------------------------------------------- #
# 配套核验
# --------------------------------------------------------------------------- #
def pair_cylinder(conn: sqlite3.Connection, cylinder_id: int, harness_code: str,
                  operator_id: int) -> dict:
    """与背架重新配套：必须核对编号一致；封存/报废瓶不可配套。"""
    cyl = _get(conn, "cylinders", cylinder_id)
    if cyl["status"] in (config.CYL_SEALED, config.CYL_SCRAPPED):
        raise SCBAError(
            f"{config.CYL_STATUS_NAMES[cyl['status']]}气瓶不能配套背架",
            code="not_issueable",
        )
    harness = _row(conn, "SELECT * FROM harnesses WHERE code=?", (harness_code,))
    if not harness:
        raise NotFound(f"背架编号 {harness_code} 不存在")
    if harness["status"] != "active":
        raise SCBAError("背架已停用")
    # 编号核对：扫描编号必须与瓶身台账当前配套一致，或显式确认改套
    if cyl["harness_id"] and cyl["harness_id"] != harness["id"]:
        current = _get(conn, "harnesses", cyl["harness_id"])
        log_event(conn, "pair_mismatch", cylinder_id=cylinder_id, actor_id=operator_id,
                  detail={"expected": current["code"], "scanned": harness_code})
        conn.commit()
        raise Conflict(
            f"错误配套：气瓶 {cyl['serial_no']} 当前登记背架为 {current['code']}，"
            f"与扫描的 {harness_code} 不一致"
        )
    conn.execute(
        "UPDATE cylinders SET harness_id=?, paired_confirmed_at=datetime('now','localtime') "
        "WHERE id=?",
        (harness["id"], cylinder_id),
    )
    log_event(conn, "pair_confirmed", cylinder_id=cylinder_id, actor_id=operator_id,
              detail={"harness": harness_code})
    conn.commit()
    return {"matched": True, "harness": harness_code}


def repaint_pair(conn: sqlite3.Connection, cylinder_id: int, harness_code: str,
                 supervisor_id: int) -> dict:
    """主管强制改套（背架损坏更换时），记录变更，仍需双方在役。"""
    sup = _person(conn, supervisor_id)
    _require_role(sup, config.ROLE_SUPERVISOR)
    cyl = _get(conn, "cylinders", cylinder_id)
    harness = _row(conn, "SELECT * FROM harnesses WHERE code=?", (harness_code,))
    if not harness:
        raise NotFound(f"背架编号 {harness_code} 不存在")
    old = cyl["harness_id"]
    conn.execute(
        "UPDATE cylinders SET harness_id=?, paired_confirmed_at=datetime('now','localtime') "
        "WHERE id=?",
        (harness["id"], cylinder_id),
    )
    log_event(conn, "pair_changed", cylinder_id=cylinder_id, actor_id=supervisor_id,
              detail={"from": old, "to": harness["code"]})
    conn.commit()
    return {"changed": True, "harness": harness_code}


# --------------------------------------------------------------------------- #
# 封存 / 报废（主管权限）
# --------------------------------------------------------------------------- #
def unseal(conn: sqlite3.Connection, cylinder_id: int, supervisor_id: int) -> dict:
    """解除封存仅装备主管。解除后瓶回到待检查，需重新检验/组批。"""
    sup = _person(conn, supervisor_id)
    _require_role(sup, config.ROLE_SUPERVISOR)
    cyl = _get(conn, "cylinders", cylinder_id)
    if cyl["status"] != config.CYL_SEALED:
        raise Conflict("仅封存状态的气瓶可以解除封存")
    conn.execute(
        "UPDATE cylinders SET status=?, seal_reason='' WHERE id=?",
        (config.CYL_RETURNED, cylinder_id),
    )
    log_event(conn, "unseal", cylinder_id=cylinder_id, actor_id=supervisor_id,
              detail="主管解除封存，转待检查")
    conn.commit()
    return {"unsealed": True}


def confirm_scrap(conn: sqlite3.Connection, cylinder_id: int, supervisor_id: int,
                  reason: str) -> dict:
    """报废确认仅装备主管。"""
    sup = _person(conn, supervisor_id)
    _require_role(sup, config.ROLE_SUPERVISOR)
    cyl = _get(conn, "cylinders", cylinder_id)
    if cyl["status"] == config.CYL_SCRAPPED:
        raise Conflict("气瓶已报废")
    conn.execute(
        "UPDATE batch_positions SET released=1, position_state='interrupted' "
        "WHERE cylinder_id=? AND released=0",
        (cylinder_id,),
    )
    conn.execute(
        "UPDATE cylinders SET status=?, scrap_reason=?, seal_reason='' WHERE id=?",
        (config.CYL_SCRAPPED, reason or "装备主管确认报废", cylinder_id),
    )
    log_event(conn, "scrap_confirmed", cylinder_id=cylinder_id, actor_id=supervisor_id,
              detail=reason)
    conn.commit()
    return {"scrapped": True}


# --------------------------------------------------------------------------- #
# 紧急借调
# --------------------------------------------------------------------------- #
def emergency_loan(conn: sqlite3.Connection, cylinder_id: int, borrower_unit: str,
                   approver_id: int) -> dict:
    """可领用瓶紧急借调出站；借调占用，不能再被领用/组批。"""
    cyl = _get(conn, "cylinders", cylinder_id)
    approver = _person(conn, approver_id)
    if cyl["status"] != config.CYL_AVAILABLE:
        raise Conflict("仅可领用状态的气瓶可以紧急借调")
    cur = conn.execute(
        """INSERT INTO loans(cylinder_id,borrower_unit,approver_id,pressure_out)
           VALUES(?,?,?,?)""",
        (cylinder_id, borrower_unit, approver_id, cyl["current_pressure"]),
    )
    conn.execute("UPDATE cylinders SET status=? WHERE id=?", (config.CYL_LOANED, cylinder_id))
    log_event(conn, "emergency_loan_out", cylinder_id=cylinder_id, actor_id=approver_id,
              detail={"unit": borrower_unit, "pressure": cyl["current_pressure"]})
    conn.commit()
    return {"loan_id": cur.lastrowid}


def loan_return_by_cylinder(conn: sqlite3.Connection, cylinder_id: int, pressure_back: float,
                            person_id: int) -> dict:
    """按气瓶找到未归还借调并归还。"""
    loan = _row(
        conn, "SELECT * FROM loans WHERE cylinder_id=? AND status='out' ORDER BY id DESC LIMIT 1",
        (cylinder_id,),
    )
    if not loan:
        raise NotFound("该气瓶没有未归还借调记录")
    return loan_return(conn, loan["id"], pressure_back, person_id)


def loan_return(conn: sqlite3.Connection, loan_id: int, pressure_back: float,
                person_id: int) -> dict:
    """借调归还：按归队流程进入待检查（生成一条归队记录）。"""
    loan = _get(conn, "loans", loan_id)
    if loan["status"] != "out":
        raise Conflict("该借调已归还")
    conn.execute(
        "UPDATE loans SET status='returned',pressure_back=?,returned_at=datetime('now','localtime')"
        " WHERE id=?",
        (pressure_back, loan_id),
    )
    conn.commit()
    res = return_cylinder(conn, loan["cylinder_id"], pressure_back, person_id,
                          note=f"紧急借调归还（{loan['borrower_unit']}）")
    log_event(conn, "emergency_loan_back", cylinder_id=loan["cylinder_id"], actor_id=person_id,
              detail={"loan_id": loan_id, "pressure": pressure_back})
    conn.commit()
    return res
