"""主管权限：解除封存、报废确认仅装备主管；其余操作角色边界。"""
from app import domain as D
from tests.base import ServiceCase


class TestSupervisorPermissions(ServiceCase):
    def setUp(self):
        super().setUp()
        self.no = self.add_cyl("SCBA-6001", status=D.ST_SEALED)

    def test_firefighter_cannot_unseal(self):
        with self.assertRaises(D.PermissionError):
            self.svc.unseal(self.no, "FF1", "解封")

    def test_dispatcher_cannot_unseal(self):
        with self.assertRaises(D.PermissionError):
            self.svc.unseal(self.no, "DP1", "解封")

    def test_inspector_cannot_unseal(self):
        with self.assertRaises(D.PermissionError):
            self.svc.unseal(self.no, "IN1", "解封")

    def test_supervisor_can_unseal(self):
        self.svc.unseal(self.no, "SUP", "复检合格")
        self.assertEqual(self.status(self.no), D.ST_PENDING_FILL)

    def test_scrap_confirm_only_supervisor(self):
        for actor in ("FF1", "DP1", "IN1"):
            with self.assertRaises(D.PermissionError):
                self.svc.confirm_scrap(self.no, actor, "报废")
        self.svc.confirm_scrap(self.no, "SUP", "评审通过")
        self.assertEqual(self.status(self.no), D.ST_SCRAPPED)

    def test_dispatcher_can_propose_but_not_confirm_scrap(self):
        no = self.add_cyl("SCBA-6002")
        self.svc.propose_condemn(no, "DP1", "瓶阀损伤无维修价值")
        self.assertEqual(self.status(no), D.ST_SEALED)
        with self.assertRaises(D.PermissionError):
            self.svc.confirm_scrap(no, "DP1", "确认")

    def test_seal_is_supervisor_only(self):
        no = self.add_cyl("SCBA-6003")
        with self.assertRaises(D.PermissionError):
            self.svc.seal(no, "DP1", "封存")
        self.svc.seal(no, "SUP", "待评估")
        self.assertEqual(self.status(no), D.ST_SEALED)

    def test_inspection_update_roles(self):
        no = self.add_cyl("SCBA-6004")
        # 战斗员、装备员不可登记检验
        with self.assertRaises(D.PermissionError):
            self.svc.update_inspection(no, "FF1", "2026-09-01")
        with self.assertRaises(D.PermissionError):
            self.svc.update_inspection(no, "DP1", "2026-09-01")
        # 检验员可以
        due = self.svc.update_inspection(no, "IN1", "2026-09-01")
        self.assertEqual(due, "2029-09-01")
        # 主管也可以
        self.svc.update_inspection(no, "SUP", "2026-09-02")

    def test_unknown_actor_rejected(self):
        with self.assertRaises(D.AuthError):
            self.svc.actor("NOBODY")

    def test_unseal_overdue_still_blocked(self):
        # 超期封存瓶：主管也不能直接解封，须先更新检验
        no = self.add_cyl("SCBA-6005", last="2022-01-01", status=D.ST_SEALED)
        self.conn.execute("UPDATE cylinders SET disposition='OVERDUE' WHERE cyl_no=?", (no,))
        self.conn.commit()
        with self.assertRaises(D.DomainError) as cm:
            self.svc.unseal(no, "SUP", "解封")
        self.assertEqual(cm.exception.code, "STILL_OVERDUE")
        # 检验员更新记录后主管可解封
        self.svc.update_inspection(no, "IN1", "2026-09-20")
        self.svc.unseal(no, "SUP", "检验合格解封")
        self.assertEqual(self.status(no), D.ST_PENDING_FILL)
