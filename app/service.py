"""业务服务层：全部状态流转、例外处置、批次排他与权限规则。"""
import datetime as dt
import json
import os
import time
import uuid

from . import domain as D
from . import db


def today():
    return dt.date.today()


def parse_date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def iso(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)


class Service:
    def __init__(self, conn, pressure_adapter, settle_seconds=900, clock=time.time):
        self.conn = conn
        self.adapter = pressure_adapter
        self.settle_seconds = settle_seconds  # 静置时长，测试可调短
        self.clock = clock

    # ================= 基础 =================
    def actor(self, actor_id):
        r = self.conn.execute(
            "SELECT * FROM personnel WHERE person_id=? AND active=1", (actor_id,)).fetchone()
        if not r:
            raise D.AuthError()
        return r

    def require_role(self, actor_id, roles):
        r = self.actor(actor_id)
        if r["role"] not in roles:
            raise D.PermissionError(f"需要角色之一，当前为「{D.ROLE_LABELS[r['role']]}」")
        return r

    def cyl(self, cyl_no):
        r = self.conn.execute("SELECT * FROM cylinders WHERE cyl_no=?", (cyl_no,)).fetchone()
        if not r:
            raise D.DomainError("CYL_NOT_FOUND", f"气瓶 {cyl_no} 不存在", http=404)
        return r

    def _set_status(self, cyl_no, status, pressure=None, disposition=None, clear_disp=False):
        sets = ["status=?"]
        vals = [status]
        if pressure is not None:
            sets.append("current_pressure=?")
            vals.append(pressure)
        if clear_disp:
            sets.append("disposition=NULL")
        elif disposition is not None:
            sets.append("disposition=?")
            vals.append(disposition)
        vals.append(cyl_no)
        self.conn.execute(f"UPDATE cylinders SET {', '.join(sets)} WHERE cyl_no=?", vals)

    def _evt(self, etype, cyl=None, actor=None, ref_type=None, ref_id=None, payload=None):
        db.append_event(self.conn, etype, cyl, actor, ref_type, ref_id, payload)

    # ================= 人员 / 任务 =================
    def list_personnel(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM personnel ORDER BY role, person_id")]

    def list_tasks(self, include_closed=False):
        sql = "SELECT * FROM tasks"
        if not include_closed:
            sql += " WHERE status='OPEN'"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY planned_at")]

    # ================= 气瓶台账 =================
    def list_cylinders(self):
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM cylinders ORDER BY cyl_no")]
        today_d = today()
        for r in rows:
            due = parse_date(r["inspection_due"])
            r["overdue"] = due < today_d
            r["due_soon"] = (not r["overdue"]) and (
                due - today_d).days <= D.INSPECTION_DUE_SOON_DAYS
            r["days_to_due"] = (due - today_d).days
            r["status_label"] = D.STATUS_LABELS[r["status"]]
            r["disposition_label"] = D.DISP.get(r["disposition"]) if r["disposition"] else None
            r["in_active_batch"] = self.conn.execute(
                "SELECT batch_id FROM active_batch_cyl WHERE cyl_no=?",
                (r["cyl_no"],)).fetchone() is not None
        return rows

    def cylinder_detail(self, cyl_no):
        c = dict(self.cyl(cyl_no))
        c["events"] = [dict(e) for e in self.conn.execute(
            "SELECT * FROM events WHERE cyl_no=? ORDER BY id DESC LIMIT 50", (cyl_no,))]
        c["checks"] = [dict(e) for e in self.conn.execute(
            """SELECT rc.* FROM return_checks rc WHERE cyl_no=? ORDER BY id DESC LIMIT 10""",
            (cyl_no,))]
        c["attachments"] = [dict(e) for e in self.conn.execute(
            "SELECT * FROM attachments WHERE cyl_no=? ORDER BY id DESC", (cyl_no,))]
        return c

    # ================= 领用 / 紧急借调 =================
    def issue(self, cyl_no, person_id, task_id=None, issue_type=D.ISSUE_TASK,
              target_unit=None, issue_pressure=None, actor_id=None):
        actor = self.actor(actor_id or person_id)
        c = self.cyl(cyl_no)
        if c["status"] != D.ST_AVAILABLE:
            raise D.DomainError(
                "NOT_AVAILABLE",
                f"气瓶 {cyl_no} 当前为「{D.STATUS_LABELS[c['status']]}」，封存/报废及在途气瓶不可领用")
        if issue_type == D.ISSUE_TASK:
            if not task_id:
                raise D.DomainError("TASK_REQUIRED", "任务领用必须指定训练任务")
            t = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not t or t["status"] != "OPEN":
                raise D.DomainError("TASK_NOT_OPEN", "训练任务不存在或已关闭")
        elif issue_type == D.ISSUE_EMERGENCY_LOAN:
            if not target_unit:
                raise D.DomainError("TARGET_UNIT_REQUIRED", "紧急借调必须登记借调单位")
        else:
            raise D.DomainError("BAD_ISSUE_TYPE", "领用类型无效")
        pressure = issue_pressure if issue_pressure is not None else c["current_pressure"]
        cur = self.conn.execute(
            """INSERT INTO issues(cyl_no,task_id,person_id,issue_type,target_unit,
               issue_pressure,issued_ts,status) VALUES(?,?,?,?,?,?,?,'OPEN')""",
            (cyl_no, task_id, person_id, issue_type, target_unit, pressure, self.clock()))
        issue_id = cur.lastrowid
        self._set_status(cyl_no, D.ST_ISSUED, pressure=pressure)
        self._evt("ISSUE", cyl_no, actor["person_id"], "issue", issue_id,
                  {"issue_type": issue_type, "task_id": task_id,
                   "target_unit": target_unit, "person_id": person_id,
                   "pressure": pressure})
        if issue_type == D.ISSUE_EMERGENCY_LOAN:
            self._evt("DISPOSITION", cyl_no, actor["person_id"], "issue", issue_id,
                      {"kind": "EMERGENCY_LOAN", "target_unit": target_unit})
        return issue_id

    # ================= 归队（去重：多人扫描只生成一条） =================
    def return_cylinder(self, cyl_no, scanner_id, remain_pressure):
        scanner = self.actor(scanner_id)
        c = self.cyl(cyl_no)
        # 多人扫描归队：已有未关闭领用 -> 只追加一条扫描审计事件，不再建记录
        open_issue = self.conn.execute(
            "SELECT * FROM issues WHERE cyl_no=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
            (cyl_no,)).fetchone()
        if c["status"] == D.ST_ISSUED and open_issue:
            self.conn.execute(
                "UPDATE issues SET returned_ts=?, return_pressure=?, scanner_id=?, status='RETURNED' WHERE id=?",
                (self.clock(), remain_pressure, scanner_id, open_issue["id"]))
            self.conn.execute(
                """INSERT INTO return_checks(issue_id,cyl_no,return_pressure,created_ts)
                   VALUES(?,?,?,?)""",
                (open_issue["id"], cyl_no, remain_pressure, self.clock()))
            self._set_status(cyl_no, D.ST_PENDING_INSPECTION, pressure=remain_pressure)
            self._evt("RETURN", cyl_no, scanner_id, "issue", open_issue["id"],
                      {"remain_pressure": remain_pressure})
            return {"created": True, "check_id": self.conn.execute(
                "SELECT id FROM return_checks WHERE issue_id=?",
                (open_issue["id"],)).fetchone()["id"]}
        if c["status"] in D.POST_RETURN_STATES:
            # 重复扫码：只记审计事件，不生成第二条归队/检查记录
            self._evt("RETURN_DUPLICATE_SCAN", cyl_no, scanner_id, None, None,
                      {"remain_pressure": remain_pressure, "current_status": c["status"]})
            return {"created": False, "reason": "DUPLICATE_SCAN",
                    "message": "该气瓶已在归队检查/充装流程中，重复扫描仅记录"}
        raise D.DomainError(
            "RETURN_BAD_STATE",
            f"气瓶 {cyl_no} 当前为「{D.STATUS_LABELS[c['status']]}」，不能扫码归队")

    # ================= 归队检查（外观 / 瓶阀 / 检验有效期） =================
    def inspect_return(self, check_id, actor_id, appearance_ok, valve_ok, note=None):
        actor = self.require_role(actor_id, D.OPS_ROLES)
        rc = self.conn.execute("SELECT * FROM return_checks WHERE id=?", (check_id,)).fetchone()
        if not rc:
            raise D.DomainError("CHECK_NOT_FOUND", "归队检查记录不存在", http=404)
        if rc["result"] != "PENDING":
            raise D.DomainError("CHECK_DONE", "该记录已完成检查")
        c = self.cyl(rc["cyl_no"])
        due = parse_date(c["inspection_due"])
        within = due >= today()
        result, disposition = "PASS", None

        if not valve_ok:
            result, disposition = "VALVE", "VALVE_DAMAGE"
        elif not appearance_ok:
            result, disposition = "APPEARANCE", "APPEARANCE_FAIL"
        elif not within:
            result, disposition = "OVERDUE", "OVERDUE"

        self.conn.execute(
            """UPDATE return_checks SET appearance_ok=?, valve_ok=?, within_due=?,
               result=?, disposition=?, inspector_id=?, inspected_ts=? WHERE id=?""",
            (int(appearance_ok), int(valve_ok), int(within), result, disposition,
             actor_id, self.clock(), check_id))

        if result == "PASS":
            # 外观合格且在检验有效期内 -> 进入待充装
            self._set_status(c["cyl_no"], D.ST_PENDING_FILL, clear_disp=True)
            self._evt("INSPECT_PASS", c["cyl_no"], actor_id, "check", check_id,
                      {"within_due": True, "note": note})
        else:
            if disposition == "OVERDUE":
                # 超期未检：封存待检验
                self._set_status(c["cyl_no"], D.ST_SEALED, disposition=disposition)
                self.conn.execute(
                    "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
                    (c["cyl_no"], "SEAL", "超期未检，封存送检", actor_id, self.clock()))
            else:
                # 瓶阀损伤 / 外观不合格：转维修
                self._set_status(c["cyl_no"], D.ST_REPAIR, disposition=disposition)
            self._evt("INSPECT_FAIL", c["cyl_no"], actor_id, "check", check_id,
                      {"result": result, "disposition": disposition, "note": note})
        return {"result": result, "disposition": disposition}

    # ================= 维修完成 =================
    def repair_done(self, cyl_no, actor_id, note=None):
        actor = self.require_role(actor_id, D.OPS_ROLES)
        c = self.cyl(cyl_no)
        if c["status"] != D.ST_REPAIR:
            raise D.DomainError("NOT_IN_REPAIR", "该气瓶不在维修中")
        self._set_status(cyl_no, D.ST_PENDING_FILL, clear_disp=True)
        self._evt("REPAIR_DONE", cyl_no, actor_id, None, None, {"note": note})

    # ================= 检验更新（检验员） =================
    def update_inspection(self, cyl_no, actor_id, last_inspection, note=None):
        actor = self.require_role(actor_id, (D.ROLE_INSPECTOR, D.ROLE_SUPERVISOR))
        self.cyl(cyl_no)
        d = parse_date(last_inspection)
        due = add_months(d, D.INSPECTION_INTERVAL_MONTHS)
        self.conn.execute(
            "UPDATE cylinders SET last_inspection=?, inspection_due=? WHERE cyl_no=?",
            (last_inspection, due.isoformat(), cyl_no))
        self._evt("INSPECTION_UPDATED", cyl_no, actor_id, None, None,
                  {"last_inspection": last_inspection, "inspection_due": due.isoformat(),
                   "note": note})
        return due.isoformat()

    # ================= 充装批次 =================
    def create_batch(self, actor_id, positions=4):
        actor = self.require_role(actor_id, D.OPS_ROLES)
        # 秒级时间戳 + 短随机后缀，避免同秒建批撞号
        bid = "PC-" + dt.datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S") \
            + "-" + uuid.uuid4().hex[:6].upper()
        self.conn.execute(
            "INSERT INTO fill_batches(id,status,created_by,created_ts) VALUES(?,?,?,?)",
            (bid, D.BATCH_OPEN, actor_id, self.clock()))
        for p in range(1, positions + 1):
            self.conn.execute(
                "INSERT INTO batch_ports(batch_id,position,status) VALUES(?,?,'WAITING')",
                (bid, p))
        self._evt("BATCH_CREATED", None, actor_id, "batch", bid, {"positions": positions})
        return bid

    def list_batches(self, active_only=False):
        sql = "SELECT * FROM fill_batches"
        if active_only:
            sql += " WHERE status IN ('OPEN','CHARGING','SETTLING')"
        out = []
        for b in self.conn.execute(sql + " ORDER BY created_ts DESC"):
            d = dict(b)
            d["ports"] = [dict(p) for p in self.conn.execute(
                "SELECT * FROM batch_ports WHERE batch_id=? ORDER BY position", (b["id"],))]
            d["status_label"] = {
                D.BATCH_OPEN: "组批中", D.BATCH_CHARGING: "充装中",
                D.BATCH_SETTLING: "静置中", D.BATCH_COMPLETED: "已完成",
                D.BATCH_ABORTED: "已中断"}[b["status"]]
            out.append(d)
        return out

    def batch_detail(self, batch_id):
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise D.DomainError("BATCH_NOT_FOUND", "充装批次不存在", http=404)
        d = dict(b)
        d["ports"] = [dict(p) for p in self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? ORDER BY position", (batch_id,))]
        d["events"] = [dict(e) for e in self.conn.execute(
            "SELECT * FROM events WHERE ref_type='batch' AND ref_id=? ORDER BY id", (batch_id,))]
        return d

    def assign_port(self, batch_id, position, cyl_no, actor_id):
        """把待充装气瓶加入批次接口。同一气瓶不能进入两个未结束批次。"""
        actor = self.require_role(actor_id, D.OPS_ROLES)
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise D.DomainError("BATCH_NOT_FOUND", "充装批次不存在", http=404)
        if b["status"] != D.BATCH_OPEN:
            raise D.DomainError("BATCH_NOT_OPEN", "批次已开始充装，不能再调整气瓶")
        port = self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? AND position=?",
            (batch_id, position)).fetchone()
        if not port:
            raise D.DomainError("PORT_NOT_FOUND", f"接口 {position} 不存在", http=404)
        c = self.cyl(cyl_no)
        # 同批内重复占用（优先于状态检查，给出精确错误码）
        dup = self.conn.execute(
            "SELECT position FROM batch_ports WHERE batch_id=? AND cyl_no=?",
            (batch_id, cyl_no)).fetchone()
        if dup:
            raise D.DomainError("ALREADY_IN_BATCH",
                                f"气瓶已在本批 {dup['position']} 号接口")
        # 排他锁：同一气瓶不能进入两个未结束批次
        lock = self.conn.execute(
            "SELECT batch_id FROM active_batch_cyl WHERE cyl_no=?", (cyl_no,)).fetchone()
        if lock:
            raise D.DomainError(
                "BATCH_EXCLUSIVITY",
                f"气瓶 {cyl_no} 已在未结束批次 {lock['batch_id']} 中，不能进入两个批次")
        if c["status"] != D.ST_PENDING_FILL:
            raise D.DomainError(
                "CYL_NOT_PENDING_FILL",
                f"气瓶 {cyl_no} 当前为「{D.STATUS_LABELS[c['status']]}」，仅待充装气瓶可入批")
        self.conn.execute("INSERT INTO active_batch_cyl(cyl_no,batch_id) VALUES(?,?)",
                          (cyl_no, batch_id))
        if port["cyl_no"]:
            # 接口已有气瓶：替换并释放原瓶
            old = port["cyl_no"]
            self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (old,))
            self._set_status(old, D.ST_PENDING_FILL)
            self._evt("PORT_UNASSIGN", old, actor_id, "port", port["id"],
                      {"batch_id": batch_id, "position": position})
        self.conn.execute(
            "UPDATE batch_ports SET cyl_no=?, status='WAITING' WHERE id=?",
            (cyl_no, port["id"]))
        self._set_status(cyl_no, D.ST_IN_BATCH)
        self._evt("PORT_ASSIGN", cyl_no, actor_id, "port", port["id"],
                  {"batch_id": batch_id, "position": position})
        return {"port": position}

    def unassign_port(self, batch_id, position, actor_id):
        actor = self.require_role(actor_id, D.OPS_ROLES)
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b or b["status"] != D.BATCH_OPEN:
            raise D.DomainError("BATCH_NOT_OPEN", "批次已开始，不能移出气瓶")
        port = self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? AND position=?",
            (batch_id, position)).fetchone()
        if not port or not port["cyl_no"]:
            raise D.DomainError("PORT_EMPTY", "该接口无气瓶")
        old = port["cyl_no"]
        self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (old,))
        self.conn.execute(
            "UPDATE batch_ports SET cyl_no=NULL,status='WAITING',final_pressure=NULL WHERE id=?",
            (port["id"],))
        self._set_status(old, D.ST_PENDING_FILL)
        self._evt("PORT_UNASSIGN", old, actor_id, "port", port["id"],
                  {"batch_id": batch_id, "position": position})

    def start_charging(self, batch_id, actor_id):
        actor = self.require_role(actor_id, D.OPS_ROLES)
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise D.DomainError("BATCH_NOT_FOUND", "充装批次不存在", http=404)
        if b["status"] != D.BATCH_OPEN:
            raise D.DomainError("BATCH_NOT_OPEN", "只有组批中的批次可以开始充装")
        ports = self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? AND cyl_no IS NOT NULL",
            (batch_id,)).fetchall()
        if not ports:
            raise D.DomainError("BATCH_EMPTY", "空批次不能开始充装")
        self.conn.execute(
            "UPDATE fill_batches SET status='CHARGING', start_ts=? WHERE id=?",
            (self.clock(), batch_id))
        for p in ports:
            self.conn.execute("UPDATE batch_ports SET status='FILLING' WHERE id=?", (p["id"],))
            self._set_status(p["cyl_no"], D.ST_FILLING)
        self._evt("BATCH_START", None, actor_id, "batch", batch_id, {"count": len(ports)})

    def complete_charging(self, batch_id, actor_id):
        """读取模拟压力台终压；终压不足的接口判废处置，其余进入静置。"""
        actor = self.require_role(actor_id, D.OPS_ROLES)
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b or b["status"] != D.BATCH_CHARGING:
            raise D.DomainError("BATCH_NOT_CHARGING", "批次不在充装中")
        ports = self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? AND cyl_no IS NOT NULL",
            (batch_id,)).fetchall()
        low = []
        for p in ports:
            c = self.cyl(p["cyl_no"])
            final = self.adapter.read_fill_pressure(p["cyl_no"], c["rated_pressure"])
            due_ts = self.clock() + self.settle_seconds
            if final < D.FINAL_MIN_MPA:
                # 终压不足：不能进入静置，释放批次锁，转维修排查
                self.conn.execute(
                    """UPDATE batch_ports SET status='FILL_LOW', final_pressure=? WHERE id=?""",
                    (final, p["id"]))
                self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (p["cyl_no"],))
                self._set_status(p["cyl_no"], D.ST_REPAIR, pressure=final,
                                 disposition="FILL_LOW")
                self._evt("FILL_LOW", p["cyl_no"], actor_id, "port", p["id"],
                          {"final_pressure": final, "min_required": D.FINAL_MIN_MPA})
                low.append({"cyl_no": p["cyl_no"], "final_pressure": final})
            else:
                self.conn.execute(
                    """UPDATE batch_ports SET status='FILLED_OK', final_pressure=?,
                       retest_initial=?, settled_due=? WHERE id=?""",
                    (final, final, due_ts, p["id"]))
                self._set_status(p["cyl_no"], D.ST_SETTLING, pressure=final)
                self._evt("FILLED", p["cyl_no"], actor_id, "port", p["id"],
                          {"final_pressure": final})
        ok = [p for p in ports if p["cyl_no"] not in {x["cyl_no"] for x in low}]
        if not ok:
            # 全部终压不足：批次直接结束（异常），不进静置
            self.conn.execute(
                "UPDATE fill_batches SET status='ABORTED', end_ts=?, abort_reason=? WHERE id=?",
                (self.clock(), "全部接口终压不足", batch_id))
            self._evt("BATCH_ABORT", None, actor_id, "batch", batch_id, {"low": low})
        else:
            self.conn.execute("UPDATE fill_batches SET status='SETTLING' WHERE id=?", (batch_id,))
            self._evt("BATCH_SETTLING", None, actor_id, "batch", batch_id,
                      {"settle_seconds": self.settle_seconds, "low": low})
        return {"low": low, "settling": len(ok)}

    def due_settlements(self):
        """到期可复测的接口（进程内提醒任务也写 reminders 表）。"""
        now = self.clock()
        return [dict(r) for r in self.conn.execute(
            """SELECT bp.* FROM batch_ports bp JOIN fill_batches fb ON fb.id=bp.batch_id
               WHERE bp.status='FILLED_OK' AND bp.settled_due<=? ORDER BY bp.settled_due""",
            (now,))]

    def retest_port(self, port_id, actor_id, force=False):
        """静置复测：压降超阈值判泄漏。force=True 用于未到期手动复测。"""
        actor = self.require_role(actor_id, D.OPS_ROLES)
        p = self.conn.execute("SELECT * FROM batch_ports WHERE id=?", (port_id,)).fetchone()
        if not p:
            raise D.DomainError("PORT_NOT_FOUND", "接口不存在", http=404)
        if p["status"] != "FILLED_OK":
            raise D.DomainError("PORT_NOT_SETTLING", "该接口不在静置待复测状态")
        if not force and self.clock() < p["settled_due"]:
            remain = int(p["settled_due"] - self.clock())
            raise D.DomainError(
                "SETTLE_NOT_DUE", f"静置未到期，还需等待约 {remain} 秒（可强制复测）")
        final = self.adapter.read_settle_pressure(p["cyl_no"], p["retest_initial"])
        drop = round(p["retest_initial"] - final, 1)
        leak = drop > D.SETTLE_DROP_MAX_MPA
        self.conn.execute(
            """UPDATE batch_ports SET status=?, retest_final=?, retest_drop=?,
               retested_ts=?, retest_by=? WHERE id=?""",
            ("RETEST_LEAK" if leak else "RETEST_PASS", final, drop,
             self.clock(), actor_id, port_id))
        self.conn.execute(
            "DELETE FROM reminders WHERE type='SETTLE_DUE' AND ref_id=?", (str(port_id),))
        if leak:
            # 复测泄漏：释放批次锁，封存待判
            self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (p["cyl_no"],))
            self._set_status(p["cyl_no"], D.ST_SEALED, pressure=final, disposition="RETEST_LEAK")
            self.conn.execute(
                "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
                (p["cyl_no"], "SEAL", "静置复测泄漏，封存待判", actor_id, self.clock()))
            self._evt("RETEST_LEAK", p["cyl_no"], actor_id, "port", port_id,
                      {"initial": p["retest_initial"], "final": final, "drop": drop})
        else:
            self._set_status(p["cyl_no"], D.ST_PENDING_PAIR, pressure=final)
            self._evt("RETEST_PASS", p["cyl_no"], actor_id, "port", port_id,
                      {"initial": p["retest_initial"], "final": final, "drop": drop})
        self._maybe_complete_batch(p["batch_id"], actor_id)
        return {"leak": leak, "initial": p["retest_initial"], "final": final, "drop": drop}

    def _maybe_complete_batch(self, batch_id, actor_id):
        pending = self.conn.execute(
            "SELECT COUNT(*) n FROM batch_ports WHERE batch_id=? AND cyl_no IS NOT NULL "
            "AND status IN ('FILLING','FILLED_OK')", (batch_id,)).fetchone()["n"]
        if pending == 0:
            self.conn.execute(
                "UPDATE fill_batches SET status='COMPLETED', end_ts=? WHERE id=? AND status='SETTLING'",
                (self.clock(), batch_id))
            # 批次结束：释放本批全部排他锁（待配套气瓶由其状态保证不重入）
            self.conn.execute(
                "DELETE FROM active_batch_cyl WHERE batch_id=?", (batch_id,))
            self._evt("BATCH_COMPLETE", None, actor_id, "batch", batch_id, None)

    def abort_batch(self, batch_id, actor_id, reason):
        """充装中断：释放全部锁，在瓶恢复待充装，终压不足/泄漏已处置的不动。"""
        actor = self.require_role(actor_id, D.OPS_ROLES)
        b = self.conn.execute("SELECT * FROM fill_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise D.DomainError("BATCH_NOT_FOUND", "充装批次不存在", http=404)
        if b["status"] not in D.BATCH_OPEN_STATES:
            raise D.DomainError("BATCH_FINISHED", "批次已结束，不能中断")
        ports = self.conn.execute(
            "SELECT * FROM batch_ports WHERE batch_id=? AND cyl_no IS NOT NULL",
            (batch_id,)).fetchall()
        released = []
        for p in ports:
            if p["status"] in ("FILL_LOW", "RETEST_LEAK", "RETEST_PASS"):
                continue  # 已完成处置/已放行配套
            self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (p["cyl_no"],))
            if p["status"] != "RETEST_PASS":
                self._set_status(p["cyl_no"], D.ST_PENDING_FILL,
                                 pressure=p["final_pressure"], clear_disp=True)
            released.append(p["cyl_no"])
            self._evt("FILL_INTERRUPTED", p["cyl_no"], actor_id, "port", p["id"],
                      {"batch_id": batch_id, "reason": reason})
        self.conn.execute(
            "UPDATE fill_batches SET status='ABORTED', end_ts=?, abort_reason=? WHERE id=?",
            (self.clock(), reason or "充装中断", batch_id))
        self._evt("BATCH_ABORT", None, actor_id, "batch", batch_id,
                  {"reason": reason, "released": released})
        return {"released": released}

    # ================= 配套核验 =================
    def pair_check(self, cyl_no, frame_scanned, actor_id):
        """与背架重新配套时核对编号；错误配套不可放行。"""
        actor = self.actor(actor_id)
        c = self.cyl(cyl_no)
        if c["status"] == D.ST_SCRAPPED:
            raise D.DomainError("SCRAPPED", "报废气瓶不可配套领用")
        if c["status"] == D.ST_SEALED:
            raise D.DomainError("SEALED", "封存气瓶不可配套领用，须先由装备主管解封")
        matched = (frame_scanned.strip() == c["frame_no"])
        self.conn.execute(
            """INSERT INTO pair_checks(cyl_no,frame_scanned,frame_expected,matched,actor_id,ts)
               VALUES(?,?,?,?,?,?)""",
            (cyl_no, frame_scanned.strip(), c["frame_no"], int(matched),
             actor_id, self.clock()))
        if not matched:
            # 错误配套：维持待配套，记录处置，不放行
            self._set_status(cyl_no, D.ST_PENDING_PAIR, disposition="PAIR_MISMATCH")
            self._evt("PAIR_MISMATCH", cyl_no, actor_id, None, None,
                      {"scanned": frame_scanned.strip(), "expected": c["frame_no"]})
            raise D.DomainError(
                "PAIR_MISMATCH",
                f"配套错误：扫描背架 {frame_scanned.strip()}，气瓶登记背架为 {c['frame_no']}")
        # 配套一致：复测合格瓶才能真正回到可领用
        if c["status"] != D.ST_PENDING_PAIR:
            # 允许在可领用状态下做复核，但不改变压力/状态
            self._evt("PAIR_RECHECK", cyl_no, actor_id, None, None, {"frame": c["frame_no"]})
            return {"matched": True, "returned_to_service": False}
        self._set_status(cyl_no, D.ST_AVAILABLE, clear_disp=True)
        self._evt("PAIR_MATCHED", cyl_no, actor_id, None, None, {"frame": c["frame_no"]})
        return {"matched": True, "returned_to_service": True}

    # ================= 封存 / 解封 / 报废（仅装备主管） =================
    def seal(self, cyl_no, actor_id, reason):
        actor = self.require_role(actor_id, (D.ROLE_SUPERVISOR,))
        c = self.cyl(cyl_no)
        if c["status"] in (D.ST_SCRAPPED,):
            raise D.DomainError("SCRAPPED", "已报废气瓶不能封存")
        self.conn.execute(
            "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
            (cyl_no, "SEAL", reason, actor_id, self.clock()))
        self._set_status(cyl_no, D.ST_SEALED)
        self._evt("SEALED", cyl_no, actor_id, None, None, {"reason": reason})

    def unseal(self, cyl_no, actor_id, reason="解除封存"):
        """解除封存只允许装备主管。超期未检瓶需先更新检验记录。"""
        actor = self.require_role(actor_id, (D.ROLE_SUPERVISOR,))
        c = self.cyl(cyl_no)
        if c["status"] != D.ST_SEALED:
            raise D.DomainError("NOT_SEALED", "该气瓶未处于封存状态")
        self.conn.execute(
            "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
            (cyl_no, "UNSEAL", reason, actor_id, self.clock()))
        if c["disposition"] == "OVERDUE" and parse_date(c["inspection_due"]) < today():
            # 检验仍过期：解封后进入待充装前仍需检验，直接置待充装会违规——保持封存提示
            raise D.DomainError(
                "STILL_OVERDUE", "检验仍超期：请先由检验员更新水压试验记录再解封")
        self._set_status(cyl_no, D.ST_PENDING_FILL, clear_disp=True)
        self._evt("UNSEALED", cyl_no, actor_id, None, None, {"reason": reason})

    def propose_condemn(self, cyl_no, actor_id, reason):
        """报废审议：封存待主管确认。"""
        actor = self.require_role(actor_id, D.OPS_ROLES)
        c = self.cyl(cyl_no)
        if c["status"] == D.ST_SCRAPPED:
            raise D.DomainError("SCRAPPED", "气瓶已报废")
        self.conn.execute(
            "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
            (cyl_no, "CONDEMN_PROPOSE", reason, actor_id, self.clock()))
        self._set_status(cyl_no, D.ST_SEALED, disposition="CONDEMN_PENDING")
        self._evt("CONDEMN_PROPOSED", cyl_no, actor_id, None, None, {"reason": reason})

    def confirm_scrap(self, cyl_no, actor_id, reason):
        """报废确认只允许装备主管。"""
        actor = self.require_role(actor_id, (D.ROLE_SUPERVISOR,))
        c = self.cyl(cyl_no)
        if c["status"] == D.ST_SCRAPPED:
            raise D.DomainError("SCRAPPED", "气瓶已报废")
        self.conn.execute(
            "INSERT INTO seal_records(cyl_no,action,reason,actor_id,ts) VALUES(?,?,?,?,?)",
            (cyl_no, "CONDEMN_CONFIRM", reason, actor_id, self.clock()))
        self.conn.execute("DELETE FROM active_batch_cyl WHERE cyl_no=?", (cyl_no,))
        self._set_status(cyl_no, D.ST_SCRAPPED, disposition="SCRAPPED")
        self._evt("SCRAPPED", cyl_no, actor_id, None, None, {"reason": reason})

    # ================= 视图数据 =================
    def view_ledger(self):
        return self.list_cylinders()

    def view_issues(self):
        rows = self.conn.execute(
            """SELECT i.*, c.frame_no, t.name task_name, p.name person_name
               FROM issues i
               JOIN cylinders c ON c.cyl_no=i.cyl_no
               LEFT JOIN tasks t ON t.task_id=i.task_id
               JOIN personnel p ON p.person_id=i.person_id
               ORDER BY i.id DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["issue_type_label"] = D.ISSUE_TYPE_LABELS[d["issue_type"]]
            d["issued_ts_str"] = iso(d["issued_ts"])
            d["returned_ts_str"] = iso(d["returned_ts"])
            out.append(d)
        return out

    def view_return_checks(self):
        rows = self.conn.execute(
            """SELECT rc.*, p.name inspector_name FROM return_checks rc
               LEFT JOIN personnel p ON p.person_id=rc.inspector_id
               ORDER BY rc.id DESC""").fetchall()
        labels = {"PENDING": "待检查", "PASS": "合格", "VALVE": "瓶阀损伤",
                  "APPEARANCE": "外观不合格", "OVERDUE": "超期未检"}
        return [dict(r, result_label=labels[r["result"]],
                     disposition_label=D.DISP.get(r["disposition"]) if r["disposition"] else None)
                for r in rows]

    def view_retests(self):
        rows = self.conn.execute(
            """SELECT bp.*, fb.status batch_status FROM batch_ports bp
               JOIN fill_batches fb ON fb.id=bp.batch_id
               WHERE bp.cyl_no IS NOT NULL AND bp.final_pressure IS NOT NULL
               ORDER BY bp.id DESC""").fetchall()
        out = []
        now = self.clock()
        for r in rows:
            d = dict(r)
            d["settled_due_str"] = iso(d["settled_due"])
            d["retested_ts_str"] = iso(d["retested_ts"])
            d["settle_remaining"] = max(0, int((d["settled_due"] or 0) - now))
            d["due"] = d["status"] == "FILLED_OK" and now >= (d["settled_due"] or 0)
            out.append(d)
        return out

    def view_pairs(self):
        rows = self.conn.execute(
            """SELECT pc.*, p.name actor_name FROM pair_checks pc
               JOIN personnel p ON p.person_id=pc.actor_id ORDER BY pc.id DESC""").fetchall()
        return [dict(r, ts_str=iso(r["ts"])) for r in rows]

    def view_due(self):
        today_d = today()
        out = []
        for c in self.list_cylinders():
            due = parse_date(c["inspection_due"])
            if c["status"] == D.ST_SCRAPPED:
                continue
            level = "OK"
            if due < today_d:
                level = "OVERDUE"
            elif (due - today_d).days <= D.INSPECTION_DUE_SOON_DAYS:
                level = "SOON"
            if level != "OK":
                out.append({**c, "level": level})
        out.sort(key=lambda x: x["days_to_due"])
        return out

    def list_reminders(self, include_acked=False):
        sql = "SELECT * FROM reminders"
        if not include_acked:
            sql += " WHERE ack=0"
        return [dict(r, ts_str=iso(r["ts"]), due_ts_str=iso(r["due_ts"]))
                for r in self.conn.execute(sql + " ORDER BY id DESC LIMIT 100")]

    def ack_reminder(self, reminder_id, actor_id):
        self.actor(actor_id)
        self.conn.execute("UPDATE reminders SET ack=1 WHERE id=?", (reminder_id,))

    def list_events(self, limit=200):
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ts_str"] = iso(d["ts"])
            out.append(d)
        return out

    # ================= 提醒扫描（后台线程周期调用） =================
    def scan_reminders(self):
        """生成静置复测到期 / 检验到期提醒，按 (type,ref_id) 去重。"""
        n = 0
        # 静置复测到期
        for p in self.due_settlements():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO reminders(type,ref_id,cyl_no,message,due_ts,ts) "
                "VALUES('SETTLE_DUE',?,?,?,?,?)",
                (str(p["id"]), p["cyl_no"],
                 f"气瓶 {p['cyl_no']} 静置期满，请进行压力复测（批次 {p['batch_id']} "
                 f"{p['position']} 号接口）",
                 p["settled_due"], self.clock()))
            n += cur.rowcount
        # 检验到期
        for c in self.view_due():
            if c["level"] == "OVERDUE":
                msg = (f"气瓶 {c['cyl_no']} 水压检验已超期 "
                       f"{-c['days_to_due']} 天，立即停止领用并送检")
                rtype = "INSPECTION_OVERDUE"
            else:
                msg = f"气瓶 {c['cyl_no']} 检验将于 {c['days_to_due']} 天后到期"
                rtype = "INSPECTION_DUE"
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO reminders(type,ref_id,cyl_no,message,due_ts,ts) "
                "VALUES(?,?,?,?,NULL,?)",
                (rtype, c["cyl_no"], c["cyl_no"], msg, self.clock()))
            n += cur.rowcount
        if n:
            self.conn.commit()
        return n
