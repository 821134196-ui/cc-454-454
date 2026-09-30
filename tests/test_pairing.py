"""配套一致性：重新配套核对编号，错误配套拦截不放行；封存/报废不可配套领用。"""
from app import domain as D
from tests.base import ServiceCase


class TestPairing(ServiceCase):
    def test_mismatch_blocks_return_to_service(self):
        no = self.cycle_to_pending_fill("SCBA-5001", frame="JZ-5001")
        _, pid_ = self.fill_and_settle(no)
        self.svc.retest_port(pid_, "DP1", force=True)
        self.assertEqual(self.status(no), D.ST_PENDING_PAIR)
        # 扫错背架
        with self.assertRaises(D.DomainError) as cm:
            self.svc.pair_check(no, "JZ-9999", "DP1")
        self.assertEqual(cm.exception.code, "PAIR_MISMATCH")
        # 状态仍是待配套且挂错误配套处置
        self.assertEqual(self.status(no), D.ST_PENDING_PAIR)
        c = self.conn.execute("SELECT disposition FROM cylinders WHERE cyl_no=?",
                              (no,)).fetchone()
        self.assertEqual(c["disposition"], "PAIR_MISMATCH")
        rec = self.conn.execute(
            "SELECT matched FROM pair_checks WHERE cyl_no=? ORDER BY id LIMIT 1",
            (no,)).fetchone()
        self.assertEqual(rec["matched"], 0)

    def test_correct_pair_clears_and_returns_available(self):
        no = self.cycle_to_pending_fill("SCBA-5002", frame="JZ-5002")
        _, pid_ = self.fill_and_settle(no)
        self.svc.retest_port(pid_, "DP1", force=True)
        # 先错后对
        with self.assertRaises(D.DomainError):
            self.svc.pair_check(no, "JZ-0000", "DP1")
        r = self.svc.pair_check(no, "JZ-5002", "DP1")
        self.assertTrue(r["returned_to_service"])
        self.assertEqual(self.status(no), D.ST_AVAILABLE)
        c = self.conn.execute("SELECT disposition FROM cylinders WHERE cyl_no=?",
                              (no,)).fetchone()
        self.assertIsNone(c["disposition"])

    def test_sealed_cannot_pair(self):
        no = self.add_cyl("SCBA-5003", frame="JZ-5003", status=D.ST_SEALED)
        with self.assertRaises(D.DomainError) as cm:
            self.svc.pair_check(no, "JZ-5003", "FF1")
        self.assertEqual(cm.exception.code, "SEALED")

    def test_scrapped_cannot_pair(self):
        no = self.add_cyl("SCBA-5004", frame="JZ-5004", status=D.ST_SCRAPPED)
        with self.assertRaises(D.DomainError) as cm:
            self.svc.pair_check(no, "JZ-5004", "DP1")
        self.assertEqual(cm.exception.code, "SCRAPPED")

    def test_available_recheck_does_not_change_state(self):
        no = self.add_cyl("SCBA-5005", frame="JZ-5005", status=D.ST_AVAILABLE)
        r = self.svc.pair_check(no, "JZ-5005", "FF1")
        self.assertTrue(r["matched"])
        self.assertFalse(r["returned_to_service"])
        self.assertEqual(self.status(no), D.ST_AVAILABLE)
