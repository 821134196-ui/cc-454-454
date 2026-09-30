"""测试夹具：内存/临时库 + 服务实例（不种样例，自行构造最小数据）。"""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import domain as D
from app.db import connect, append_event
from app.pressure import MockPressureAdapter
from app.service import Service, add_months


class ServiceCase(unittest.TestCase):
    SETTLE = 60  # 测试一律 force 复测，时长无关

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = connect(os.path.join(self.dir, "t.db"))
        self.adapter = MockPressureAdapter()
        self.svc = Service(self.conn, self.adapter, settle_seconds=self.SETTLE)
        self._seed_people()

    def tearDown(self):
        self.conn.close()

    def _seed_people(self):
        rows = [
            ("FF1", "战斗员甲", D.ROLE_FIREFIGHTER, "一站"),
            ("DP1", "装备员甲", D.ROLE_DISPATCHER, "一站"),
            ("IN1", "检验员甲", D.ROLE_INSPECTOR, "检验站"),
            ("SUP", "主管甲", D.ROLE_SUPERVISOR, "装备股"),
        ]
        for pid, name, role, unit in rows:
            self.conn.execute(
                "INSERT INTO personnel(person_id,name,role,unit,active) VALUES(?,?,?,?,1)",
                (pid, name, role, unit))
        self.conn.execute(
            "INSERT INTO tasks(task_id,name,task_type,location,planned_at,status) "
            "VALUES('T1','训练任务','灭火','训练场','2026-10-01 09:00','OPEN')")
        self.conn.commit()

    def add_cyl(self, no="SCBA-1001", frame="JZ-1001", last="2025-01-01",
                status=D.ST_AVAILABLE, pressure=30.0):
        due = add_months(dt.date.fromisoformat(last), D.INSPECTION_INTERVAL_MONTHS)
        import time
        self.conn.execute(
            """INSERT INTO cylinders(cyl_no,frame_no,rated_pressure,volume_l,material,
               last_inspection,inspection_due,status,current_pressure,created_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (no, frame, D.RATED_PRESSURE_MPA, 6.8, "碳纤维全缠绕",
             last, due.isoformat(), status, pressure, time.time()))
        self.conn.commit()
        return no

    def cycle_to_pending_fill(self, no, frame="JZ-1001", last="2025-01-01"):
        """领用 -> 归队 -> 检查合格，得到待充装气瓶。"""
        self.add_cyl(no, frame, last)
        self.svc.issue(no, "FF1", task_id="T1", actor_id="DP1")
        self.svc.return_cylinder(no, "FF1", 20.0)
        cid = self.conn.execute(
            "SELECT id FROM return_checks WHERE cyl_no=?", (no,)).fetchone()["id"]
        self.svc.inspect_return(cid, "DP1", True, True)
        return no

    def fill_and_settle(self, no, position=1):
        """建批上瓶 -> 充装 -> 读终压，返回 (batch_id, port_id)，瓶静置中。"""
        bid = self.svc.create_batch("DP1", positions=2)
        self.svc.assign_port(bid, position, no, "DP1")
        self.svc.start_charging(bid, "DP1")
        self.svc.complete_charging(bid, "DP1")
        pid_ = self.conn.execute(
            "SELECT id FROM batch_ports WHERE batch_id=? AND cyl_no=?", (bid, no)).fetchone()["id"]
        return bid, pid_

    def status(self, no):
        return self.conn.execute(
            "SELECT status FROM cylinders WHERE cyl_no=?", (no,)).fetchone()["status"]
