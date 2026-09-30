"""批次排他：同一气瓶不能进入两个未结束批次；批次结束/中断释放后可再入批。"""
from app import domain as D
from tests.base import ServiceCase


class TestBatchExclusivity(ServiceCase):
    def test_cannot_enter_two_active_batches(self):
        no = self.cycle_to_pending_fill("SCBA-2001")
        b1 = self.svc.create_batch("DP1", positions=2)
        b2 = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(b1, 1, no, "DP1")
        # 第二个未结束批次拒绝
        with self.assertRaises(D.DomainError) as cm:
            self.svc.assign_port(b2, 1, no, "DP1")
        self.assertEqual(cm.exception.code, "BATCH_EXCLUSIVITY")
        # 排他锁确实存在
        lock = self.conn.execute(
            "SELECT batch_id FROM active_batch_cyl WHERE cyl_no=?", (no,)).fetchone()
        self.assertEqual(lock["batch_id"], b1)

    def test_exclusivity_holds_during_charging_and_settling(self):
        no = self.cycle_to_pending_fill("SCBA-2002")
        b1, _ = self.fill_and_settle(no)  # 已进入静置（批次未结束）
        b2 = self.svc.create_batch("DP1", positions=2)
        # 未结束批次互斥优先报排他错误码
        with self.assertRaises(D.DomainError) as cm:
            self.svc.assign_port(b2, 1, no, "DP1")
        self.assertEqual(cm.exception.code, "BATCH_EXCLUSIVITY")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM active_batch_cyl WHERE cyl_no=?", (no,)).fetchone())

    def test_lock_released_after_full_completion(self):
        no = self.cycle_to_pending_fill("SCBA-2003")
        b1, pid_ = self.fill_and_settle(no)
        self.svc.retest_port(pid_, "DP1", force=True)
        # 批次自动完成，锁释放；气瓶到待配套（不能重入充装，由状态保证）
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM active_batch_cyl WHERE cyl_no=?", (no,)).fetchone())
        batch = self.conn.execute(
            "SELECT status FROM fill_batches WHERE id=?", (b1,)).fetchone()["status"]
        self.assertEqual(batch, D.BATCH_COMPLETED)
        self.assertEqual(self.status(no), D.ST_PENDING_PAIR)

    def test_lock_released_after_abort_and_reassign(self):
        no = self.cycle_to_pending_fill("SCBA-2004")
        b1 = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(b1, 1, no, "DP1")
        self.svc.start_charging(b1, "DP1")
        self.svc.abort_batch(b1, "DP1", "停电中断")
        # 中断释放锁、退回待充装
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM active_batch_cyl WHERE cyl_no=?", (no,)).fetchone())
        self.assertEqual(self.status(no), D.ST_PENDING_FILL)
        # 可进入新批次
        b2 = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(b2, 1, no, "DP1")
        self.assertEqual(self.status(no), D.ST_IN_BATCH)

    def test_same_batch_duplicate_assign_rejected(self):
        no = self.cycle_to_pending_fill("SCBA-2005")
        b = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(b, 1, no, "DP1")
        with self.assertRaises(D.DomainError) as cm:
            self.svc.assign_port(b, 2, no, "DP1")
        self.assertEqual(cm.exception.code, "ALREADY_IN_BATCH")

    def test_low_fill_port_releases_lock_without_blocking_others(self):
        no1 = self.cycle_to_pending_fill("SCBA-2006", frame="JZ-2006")
        no2 = self.cycle_to_pending_fill("SCBA-2007", frame="JZ-2007")
        self.adapter.inject_fault(no1, D.FAULT_LOW_FILL)
        b = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(b, 1, no1, "DP1")
        self.svc.assign_port(b, 2, no2, "DP1")
        self.svc.start_charging(b, "DP1")
        r = self.svc.complete_charging(b, "DP1")
        self.assertEqual([x["cyl_no"] for x in r["low"]], [no1])
        # 终压不足瓶锁已释放并转维修；合格瓶仍持锁静置
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM active_batch_cyl WHERE cyl_no=?", (no1,)).fetchone())
        self.assertEqual(self.status(no1), D.ST_REPAIR)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM active_batch_cyl WHERE cyl_no=?", (no2,)).fetchone())
