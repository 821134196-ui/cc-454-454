#!/usr/bin/env python3
"""SCBA 领域规则测试。

覆盖必测项：
  1. 批次排他      2. 压力阈值      3. 重复归队
  4. 配套一致性    5. 主管权限
另含七类处置：瓶阀损伤 / 超期未检 / 终压不足 / 复测泄漏 / 错误配套 / 充装中断 / 紧急借调。

运行：python3 -m unittest -v   或   python3 tests.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scba import config, dates, service
from scba.adapter import PressureBenchAdapter
from scba.engine import Engine
from scba.errors import Conflict, PermissionDenied, SCBAError
from scba.schema import connect, init_db
from scba.seed import seed

FIGHTER1, FIGHTER2, OPERATOR, SUPERVISOR = 1, 2, 5, 6  # 见 seed.PEOPLE 顺序


class DBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "t.db"
        init_db(self.db)
        seed(self.db)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def cyl(self, serial):
        return self.conn.execute("SELECT * FROM cylinders WHERE serial_no=?",
                                 (serial,)).fetchone()

    def cid(self, serial):
        return self.cyl(serial)["id"]

    # ---------- 工具：归队 + 外观合格 ----------
    def make_ready(self, serial="QP-6801", pressure=9.0):
        cid = self.cid(serial)
        service.return_cylinder(self.conn, cid, pressure, FIGHTER1)
        rid = self.conn.execute(
            "SELECT id FROM returns WHERE cylinder_id=? ORDER BY id DESC LIMIT 1", (cid,)
        ).fetchone()["id"]
        service.inspect_appearance(self.conn, rid, appearance_ok=True, valve_ok=True,
                                   inspector_id=OPERATOR)
        return cid

    # ================================================================== #
    # 1. 批次排他
    # ================================================================== #
    def test_batch_exclusivity_service(self):
        cid = self.make_ready("QP-6801")
        b1 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, b1, cid)
        # 第二个未结束批次不能再加入同一气瓶
        b2 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        with self.assertRaises(Conflict):
            service.add_to_batch(self.conn, b2, cid)

    def test_batch_exclusivity_db_constraint(self):
        """DB 部分唯一索引 uq_position_active 兜底，绕过服务层也不能双占。"""
        cid = self.make_ready("QP-6802")
        b1 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        b2 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, b1, cid, position=1)
        with self.assertRaises(Exception):
            self.conn.execute(
                "INSERT INTO batch_positions(batch_id,position,cylinder_id) VALUES(?,?,?)",
                (b2, 1, cid))

    def test_cylinder_can_enter_new_batch_after_release(self):
        """中断释放后可进入新批次。"""
        cid = self.make_ready("QP-6804")
        b1 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, b1, cid)
        service.interrupt_batch(self.conn, b1, "测试中断", OPERATOR)
        # 中断后恢复待检查，需重新外观检查
        service.inspect_appearance(
            self.conn,
            self.conn.execute("SELECT id FROM returns WHERE cylinder_id=? ORDER BY id DESC LIMIT 1",
                              (cid,)).fetchone()["id"],
            appearance_ok=True, valve_ok=True, inspector_id=OPERATOR)
        b2 = service.create_batch(self.conn, OPERATOR)["batch_id"]
        pos = service.add_to_batch(self.conn, b2, cid)
        self.assertEqual(pos["position"], 1)

    # ================================================================== #
    # 2. 压力阈值
    # ================================================================== #
    def test_final_pressure_below_min_sealed(self):
        bench = PressureBenchAdapter.for_scenario("low")
        final = bench.final_reading(8.0)
        self.assertLess(final, config.FILL_FINAL_MIN)

        cid = self.make_ready("QP-6805")
        bid = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, bid, cid, scenario="low")
        service.start_fill(self.conn, bid, OPERATOR)
        res = service.complete_fill(self.conn, bid, OPERATOR)
        self.assertIn(self.cyl("QP-6805")["serial_no"], res["low_final"])
        self.assertEqual(self.cyl("QP-6805")["status"], config.CYL_SEALED)

    def test_retest_leak_threshold(self):
        bench = PressureBenchAdapter(leak=True)
        drop = bench.fill_target() - bench.settle_reading(bench.fill_target(), 60)
        self.assertGreater(round(drop, 2), config.RETEST_DROP_LIMIT)

        cid = self.make_ready("QP-6801")
        bid = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, bid, cid, scenario="leak")
        service.start_fill(self.conn, bid, OPERATOR)
        service.complete_fill(self.conn, bid, OPERATOR)
        pos = self.conn.execute(
            "SELECT id FROM batch_positions WHERE batch_id=? AND cylinder_id=?", (bid, cid)
        ).fetchone()
        r = service.run_retest(self.conn, pos["id"], OPERATOR, elapsed_s=60)
        self.assertTrue(r["leak"])
        self.assertEqual(self.cyl("QP-6801")["status"], config.CYL_SEALED)

    def test_retest_stable_returns_to_available(self):
        cid = self.make_ready("QP-6802")
        bid = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, bid, cid)  # 正常适配器
        service.start_fill(self.conn, bid, OPERATOR)
        service.complete_fill(self.conn, bid, OPERATOR)
        pos = self.conn.execute(
            "SELECT id FROM batch_positions WHERE batch_id=? AND cylinder_id=?", (bid, cid)
        ).fetchone()
        r = service.run_retest(self.conn, pos["id"], OPERATOR, elapsed_s=config.RETEST_SETTLE_SECONDS)
        self.assertFalse(r["leak"])
        self.assertLessEqual(r["drop"], config.RETEST_DROP_LIMIT)
        self.assertEqual(self.cyl("QP-6802")["status"], config.CYL_AVAILABLE)
        self.assertGreaterEqual(self.cyl("QP-6802")["current_pressure"], config.FILL_FINAL_MIN)

    def test_return_low_pressure_flag(self):
        cid = self.cid("QP-6804")  # 种子中可领用
        r = service.return_cylinder(self.conn, cid, config.RETURN_LOW_PRESSURE - 0.5, FIGHTER1)
        self.assertTrue(r["low_pressure"])

    # ================================================================== #
    # 3. 重复归队
    # ================================================================== #
    def test_duplicate_return_single_record(self):
        cid = self.cid("QP-6804")
        service.issue_cylinder(self.conn, cid, 2, FIGHTER1)
        r1 = service.return_cylinder(self.conn, cid, 9.0, FIGHTER1)
        r2 = service.return_cylinder(self.conn, cid, 8.0, FIGHTER2)  # 另一人重复扫
        r3 = service.return_cylinder(self.conn, cid, 7.0, FIGHTER1)
        self.assertFalse(r1["dedup"])
        self.assertTrue(r2["dedup"])
        self.assertTrue(r3["dedup"])
        self.assertEqual(r1["return_id"], r2["return_id"])
        n = self.conn.execute(
            "SELECT COUNT(*) c, MAX(dup_scan_count) m FROM returns WHERE cylinder_id=? AND status='pending'",
            (cid,)).fetchone()
        self.assertEqual(n["c"], 1)
        self.assertEqual(n["m"], 2)

    # ================================================================== #
    # 4. 配套一致性
    # ================================================================== #
    def test_pair_mismatch_rejected(self):
        cid = self.cid("QP-6801")  # 配套 BJ-A01
        with self.assertRaises(Conflict):
            service.pair_cylinder(self.conn, cid, "BJ-A02", OPERATOR)
        ev = self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE cylinder_id=? AND type='pair_mismatch'",
            (cid,)).fetchone()["c"]
        self.assertGreaterEqual(ev, 1)

    def test_pair_match_ok(self):
        cid = self.cid("QP-6801")
        r = service.pair_cylinder(self.conn, cid, "BJ-A01", OPERATOR)
        self.assertTrue(r["matched"])

    def test_pair_blocked_for_sealed_scrapped(self):
        sealed = self.cyl("QP-6814")   # 种子：超期封存
        scrapped = self.cyl("QP-6816")  # 种子：报废
        self.assertEqual(sealed["status"], config.CYL_SEALED)
        self.assertEqual(scrapped["status"], config.CYL_SCRAPPED)
        with self.assertRaises(SCBAError):
            service.pair_cylinder(self.conn, sealed["id"], "BJ-A01", OPERATOR)
        with self.assertRaises(SCBAError):
            service.pair_cylinder(self.conn, scrapped["id"], "BJ-A01", OPERATOR)

    def test_supervisor_force_repair_pair(self):
        cid = self.cid("QP-6801")
        r = service.repaint_pair(self.conn, cid, "BJ-A05", SUPERVISOR)
        self.assertTrue(r["changed"])
        self.assertEqual(self.cyl("QP-6801")["harness_id"],
                         self.conn.execute("SELECT id FROM harnesses WHERE code='BJ-A05'"
                                           ).fetchone()["id"])

    # ================================================================== #
    # 5. 主管权限
    # ================================================================== #
    def test_unseal_only_supervisor(self):
        cid = self.cid("QP-6814")  # 封存
        with self.assertRaises(PermissionDenied):
            service.unseal(self.conn, cid, FIGHTER1)
        with self.assertRaises(PermissionDenied):
            service.unseal(self.conn, cid, OPERATOR)
        r = service.unseal(self.conn, cid, SUPERVISOR)
        self.assertTrue(r["unsealed"])
        self.assertEqual(self.cyl("QP-6814")["status"], config.CYL_RETURNED)

    def test_scrap_only_supervisor(self):
        cid = self.cid("QP-6813")  # 可领用
        with self.assertRaises(PermissionDenied):
            service.confirm_scrap(self.conn, cid, OPERATOR, "测试")
        service.confirm_scrap(self.conn, cid, SUPERVISOR, "到龄报废")
        self.assertEqual(self.cyl("QP-6813")["status"], config.CYL_SCRAPPED)

    def test_repaint_only_supervisor(self):
        with self.assertRaises(PermissionDenied):
            service.repaint_pair(self.conn, self.cid("QP-6802"), "BJ-A09", OPERATOR)

    # ================================================================== #
    # 其余处置：外观/超期/中断/借调/封存报废不可领用
    # ================================================================== #
    def test_valve_damage_seals(self):
        cid = self.cid("QP-6805")
        service.return_cylinder(self.conn, cid, 10.0, FIGHTER1)
        rid = self.conn.execute("SELECT id FROM returns ORDER BY id DESC LIMIT 1").fetchone()["id"]
        res = service.inspect_appearance(self.conn, rid, appearance_ok=True, valve_ok=False,
                                         findings="瓶阀崩缺", inspector_id=OPERATOR)
        self.assertEqual(res["result"], "valve_damage")
        self.assertEqual(self.cyl("QP-6805")["status"], config.CYL_SEALED)

    def test_overdue_inspection_seals(self):
        cyl = self.cyl("QP-6814")
        self.assertEqual(cyl["status"], config.CYL_SEALED)
        self.assertIn("超期", cyl["seal_reason"])

    def test_interrupt_releases_and_requeues(self):
        cid = self.make_ready("QP-6801")
        bid = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, bid, cid)
        service.start_fill(self.conn, bid, OPERATOR)
        r = service.interrupt_batch(self.conn, bid, "停电", OPERATOR)
        self.assertEqual(r["released"], 1)
        self.assertEqual(self.cyl("QP-6801")["status"], config.CYL_RETURNED)
        self.assertEqual(
            self.conn.execute("SELECT status FROM batches WHERE id=?", (bid,)
                              ).fetchone()["status"], config.BATCH_INTERRUPTED)

    def test_emergency_loan_flow(self):
        cid = self.cid("QP-6802")
        loan = service.emergency_loan(self.conn, cid, "兄弟站", SUPERVISOR)
        self.assertEqual(self.cyl("QP-6802")["status"], config.CYL_LOANED)
        # 借调中不可领用
        with self.assertRaises(SCBAError):
            service.issue_cylinder(self.conn, cid, 1, FIGHTER1)
        service.loan_return(self.conn, loan["loan_id"], 7.0, FIGHTER1)
        self.assertEqual(self.cyl("QP-6802")["status"], config.CYL_RETURNED)

    def test_sealed_scrapped_not_issueable(self):
        with self.assertRaises(SCBAError):
            service.issue_cylinder(self.conn, self.cid("QP-6814"), 1, FIGHTER1)
        with self.assertRaises(SCBAError):
            service.issue_cylinder(self.conn, self.cid("QP-6816"), 1, FIGHTER1)

    def test_engine_auto_retest_and_expiry_reminder(self):
        eng = Engine(self.db, interval=999)
        cid = self.make_ready("QP-6801")
        bid = service.create_batch(self.conn, OPERATOR)["batch_id"]
        service.add_to_batch(self.conn, bid, cid, scenario="leak")
        service.start_fill(self.conn, bid, OPERATOR)
        service.complete_fill(self.conn, bid, OPERATOR)
        self.conn.execute(
            "UPDATE reminders SET due_at=datetime('now','localtime') WHERE cylinder_id=?", (cid,))
        self.conn.commit()
        fired = eng.process_due_retests(self.conn)
        self.conn.commit()
        self.assertGreaterEqual(fired, 1)
        self.assertEqual(self.cyl("QP-6801")["status"], config.CYL_SEALED)
        # 到期提醒（种子中存在超期/临期瓶）
        made = eng.generate_expiry_reminders(self.conn)
        self.conn.commit()
        self.assertGreaterEqual(made, 1)

    def test_seed_samples_present(self):
        """启动样例：泄漏、借调、报废各至少一条。"""
        self.assertEqual(self.cyl("QP-6812")["status"], config.CYL_LOANED)
        self.assertEqual(self.cyl("QP-6816")["status"], config.CYL_SCRAPPED)
        leak = self.conn.execute(
            "SELECT COUNT(*) c FROM batch_positions WHERE leak_result='leak'").fetchone()["c"]
        self.assertGreaterEqual(leak, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
