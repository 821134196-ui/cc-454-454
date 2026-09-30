"""其余处置分支：瓶阀损伤/外观不合格、超期未检、紧急借调、检验到期视图与提醒。"""
from app import domain as D
from tests.base import ServiceCase


class TestDispositions(ServiceCase):
    def _returned(self, no):
        self.add_cyl(no)
        self.svc.issue(no, "FF1", task_id="T1", actor_id="DP1")
        self.svc.return_cylinder(no, "FF1", 20.0)
        return self.conn.execute(
            "SELECT id FROM return_checks WHERE cyl_no=?", (no,)).fetchone()["id"]

    def test_valve_damage_goes_repair(self):
        cid = self._returned("SCBA-7001")
        r = self.svc.inspect_return(cid, "DP1", appearance_ok=True, valve_ok=False)
        self.assertEqual(r["result"], "VALVE")
        self.assertEqual(self.status("SCBA-7001"), D.ST_REPAIR)

    def test_appearance_fail_goes_repair(self):
        cid = self._returned("SCBA-7002")
        r = self.svc.inspect_return(cid, "DP1", appearance_ok=False, valve_ok=True)
        self.assertEqual(r["result"], "APPEARANCE")
        self.assertEqual(self.status("SCBA-7002"), D.ST_REPAIR)

    def test_overdue_on_return_seals(self):
        # 上次检测 2022-01-01 -> 2025-01-01 到期，当前已超期
        no = "SCBA-7003"
        self.add_cyl(no, last="2022-01-01")
        self.svc.issue(no, "FF1", task_id="T1", actor_id="DP1")
        self.svc.return_cylinder(no, "FF1", 20.0)
        cid = self.conn.execute(
            "SELECT id FROM return_checks WHERE cyl_no=?", (no,)).fetchone()["id"]
        r = self.svc.inspect_return(cid, "DP1", True, True)
        self.assertEqual(r["result"], "OVERDUE")
        self.assertEqual(self.status(no), D.ST_SEALED)

    def test_pass_enters_pending_fill(self):
        cid = self._returned("SCBA-7004")
        r = self.svc.inspect_return(cid, "DP1", True, True)
        self.assertEqual(r["result"], "PASS")
        self.assertEqual(self.status("SCBA-7004"), D.ST_PENDING_FILL)

    def test_emergency_loan_requires_unit(self):
        no = self.add_cyl("SCBA-7005")
        with self.assertRaises(D.DomainError) as cm:
            self.svc.issue(no, "FF1", issue_type=D.ISSUE_EMERGENCY_LOAN,
                          target_unit=None, actor_id="SUP")
        self.assertEqual(cm.exception.code, "TARGET_UNIT_REQUIRED")

    def test_emergency_loan_flow(self):
        no = self.add_cyl("SCBA-7006")
        iid = self.svc.issue(no, "FF1", issue_type=D.ISSUE_EMERGENCY_LOAN,
                             target_unit="外站", actor_id="SUP")
        issue = self.conn.execute("SELECT * FROM issues WHERE id=?", (iid,)).fetchone()
        self.assertEqual(issue["issue_type"], D.ISSUE_EMERGENCY_LOAN)
        self.assertEqual(self.status(no), D.ST_ISSUED)
        # 借调归还同样走检查流程
        self.svc.return_cylinder(no, "FF1", 6.0)
        self.assertEqual(self.status(no), D.ST_PENDING_INSPECTION)

    def test_task_issue_requires_open_task(self):
        no = self.add_cyl("SCBA-7007")
        with self.assertRaises(D.DomainError):
            self.svc.issue(no, "FF1", task_id="NOT-EXIST", actor_id="DP1")

    def test_repair_done_returns_to_pending_fill(self):
        cid = self._returned("SCBA-7008")
        self.svc.inspect_return(cid, "DP1", False, True)
        self.svc.repair_done("SCBA-7008", "DP1", "修复")
        self.assertEqual(self.status("SCBA-7008"), D.ST_PENDING_FILL)

    def test_due_view_levels(self):
        import datetime as dt
        from app.service import add_months
        # 超期
        self.add_cyl("SCBA-7101", last="2022-01-01")
        # 临近：有效期 = 今天 +10 天 -> 上次检测 = 今天+10天-36月
        future_due = dt.date.today() + dt.timedelta(days=10)
        last_soon = add_months(future_due, -36)
        self.add_cyl("SCBA-7102", last=last_soon.isoformat())
        due = {x["cyl_no"]: x["level"] for x in self.svc.view_due()}
        self.assertEqual(due.get("SCBA-7101"), "OVERDUE")
        self.assertEqual(due.get("SCBA-7102"), "SOON")

    def test_reminders_dedup(self):
        no = self.cycle_to_pending_fill("SCBA-7103", frame="JZ-7103")
        # settle_seconds=60，直接把到期时间改到过去
        _, pid_ = self.fill_and_settle(no)
        self.conn.execute("UPDATE batch_ports SET settled_due=0 WHERE id=?", (pid_,))
        self.conn.commit()
        n1 = self.svc.scan_reminders()
        n2 = self.svc.scan_reminders()
        self.assertGreaterEqual(n1, 1)
        self.assertEqual(n2, 0)  # (type,ref_id) 去重
        rows = self.conn.execute(
            "SELECT COUNT(*) n FROM reminders WHERE type='SETTLE_DUE' AND ref_id=?",
            (str(pid_),)).fetchone()["n"]
        self.assertEqual(rows, 1)
