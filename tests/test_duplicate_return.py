"""重复归队：多人/多次扫描只生成一条归队检查记录。"""
from app import domain as D
from tests.base import ServiceCase


class TestDuplicateReturn(ServiceCase):
    def _issue(self, no="SCBA-4001"):
        self.add_cyl(no)
        self.svc.issue(no, "FF1", task_id="T1", actor_id="DP1")
        return no

    def test_first_scan_creates_one_check(self):
        no = self._issue()
        r = self.svc.return_cylinder(no, "FF1", 18.0)
        self.assertTrue(r["created"])
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM return_checks rc JOIN issues i ON i.id=rc.issue_id "
            "WHERE rc.cyl_no=?", (no,)).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(self.status(no), D.ST_PENDING_INSPECTION)
        # 领用单已置 RETURNED
        self.assertEqual(self.conn.execute(
            "SELECT status FROM issues WHERE cyl_no=?", (no,)).fetchone()["status"], "RETURNED")

    def test_multi_person_rescan_makes_no_new_record(self):
        no = self._issue()
        self.svc.return_cylinder(no, "FF1", 18.0)
        # 另一名战斗员对同一在途后气瓶重复扫描
        r2 = self.svc.return_cylinder(no, "FF1", 17.5)
        self.assertFalse(r2["created"])
        self.assertEqual(r2["reason"], "DUPLICATE_SCAN")
        # 处于检查、待充装、静置等任何在途阶段重复扫码，都不新增记录
        cid = self.conn.execute(
            "SELECT id FROM return_checks WHERE cyl_no=?", (no,)).fetchone()["id"]
        self.svc.inspect_return(cid, "DP1", True, True)
        r3 = self.svc.return_cylinder(no, "FF1", 17.0)
        self.assertFalse(r3["created"])
        n_checks = self.conn.execute(
            "SELECT COUNT(*) n FROM return_checks WHERE cyl_no=?", (no,)).fetchone()["n"]
        n_issues = self.conn.execute(
            "SELECT COUNT(*) n FROM issues WHERE cyl_no=?", (no,)).fetchone()["n"]
        self.assertEqual(n_checks, 1)
        self.assertEqual(n_issues, 1)
        # 重复扫描留有审计事件
        audits = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE cyl_no=? AND type='RETURN_DUPLICATE_SCAN'",
            (no,)).fetchone()["n"]
        self.assertGreaterEqual(audits, 2)

    def test_rescan_does_not_overwrite_pressure(self):
        no = self._issue()
        self.svc.return_cylinder(no, "FF1", 18.0)
        self.svc.return_cylinder(no, "FF1", 5.0)  # 重复扫描携带不同余压
        rc = self.conn.execute(
            "SELECT return_pressure FROM return_checks WHERE cyl_no=?", (no,)).fetchone()
        self.assertEqual(rc["return_pressure"], 18.0)

    def test_return_from_wrong_state_rejected(self):
        no = self.add_cyl("SCBA-4002", status=D.ST_AVAILABLE)
        with self.assertRaises(D.DomainError) as cm:
            self.svc.return_cylinder(no, "FF1", 10.0)
        self.assertEqual(cm.exception.code, "RETURN_BAD_STATE")
