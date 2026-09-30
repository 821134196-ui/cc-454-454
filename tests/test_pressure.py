"""压力阈值：终压不足 <28.5MPa 转维修；静置压降 >1.0MPa 判泄漏封存；合格回可领用。"""
from app import domain as D
from tests.base import ServiceCase


class TestPressureThresholds(ServiceCase):
    def test_final_pressure_below_threshold_is_low_fill(self):
        no = self.cycle_to_pending_fill("SCBA-3001")
        self.adapter.inject_fault(no, D.FAULT_LOW_FILL)
        _, pid_ = self.fill_and_settle(no)
        port = self.conn.execute("SELECT * FROM batch_ports WHERE id=?", (pid_,)).fetchone()
        self.assertEqual(port["status"], D.PORT_FILL_LOW)
        self.assertLess(port["final_pressure"], D.FINAL_MIN_MPA)
        self.assertEqual(port["final_pressure"], 26.3)
        self.assertEqual(self.status(no), D.ST_REPAIR)

    def test_normal_fill_meets_threshold(self):
        no = self.cycle_to_pending_fill("SCBA-3002")
        bid, pid_ = self.fill_and_settle(no)
        port = self.conn.execute("SELECT * FROM batch_ports WHERE id=?", (pid_,)).fetchone()
        self.assertEqual(port["status"], D.PORT_FILLED_OK)
        self.assertGreaterEqual(port["final_pressure"], D.FINAL_MIN_MPA)
        self.assertEqual(self.status(no), D.ST_SETTLING)

    def test_settle_drop_over_limit_is_leak_and_sealed(self):
        no = self.cycle_to_pending_fill("SCBA-3003")
        self.adapter.inject_fault(no, D.FAULT_LEAK)
        _, pid_ = self.fill_and_settle(no)
        r = self.svc.retest_port(pid_, "DP1", force=True)
        self.assertTrue(r["leak"])
        self.assertGreater(r["drop"], D.SETTLE_DROP_MAX_MPA)
        self.assertEqual(r["initial"] - r["final"], 3.5)
        self.assertEqual(self.status(no), D.ST_SEALED)
        # 泄漏处置挂起，锁释放
        c = self.conn.execute("SELECT disposition FROM cylinders WHERE cyl_no=?",
                              (no,)).fetchone()
        self.assertEqual(c["disposition"], "RETEST_LEAK")

    def test_stable_pressure_passes_then_pair_returns_to_available(self):
        no = self.cycle_to_pending_fill("SCBA-3004", frame="JZ-3004")
        _, pid_ = self.fill_and_settle(no)
        r = self.svc.retest_port(pid_, "DP1", force=True)
        self.assertFalse(r["leak"])
        self.assertLessEqual(r["drop"], D.SETTLE_DROP_MAX_MPA)
        self.assertEqual(self.status(no), D.ST_PENDING_PAIR)
        # 配套正确后回到可领用
        pr = self.svc.pair_check(no, "JZ-3004", "DP1")
        self.assertTrue(pr["matched"])
        self.assertTrue(pr["returned_to_service"])
        self.assertEqual(self.status(no), D.ST_AVAILABLE)

    def test_retest_before_settle_due_rejected_without_force(self):
        no = self.cycle_to_pending_fill("SCBA-3005")
        _, pid_ = self.fill_and_settle(no)
        with self.assertRaises(D.DomainError) as cm:
            self.svc.retest_port(pid_, "DP1", force=False)
        self.assertEqual(cm.exception.code, "SETTLE_NOT_DUE")

    def test_all_low_batch_aborts(self):
        no1 = self.cycle_to_pending_fill("SCBA-3006", frame="JZ-3006")
        self.adapter.inject_fault(no1, D.FAULT_LOW_FILL)
        b, _ = self.fill_and_settle(no1)
        batch = self.conn.execute(
            "SELECT status,abort_reason FROM fill_batches WHERE id=?", (b,)).fetchone()
        self.assertEqual(batch["status"], D.BATCH_ABORTED)
        self.assertIn("终压不足", batch["abort_reason"])
