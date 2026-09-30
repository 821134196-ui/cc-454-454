"""进程内后台引擎。

职责：
1. 静置复测：批次充装完成后按 retest_due_at 定时自动执行模拟复测（压力稳定回库 / 泄漏封存）。
2. 检验到期：扫描气瓶检验有效期，提前 EXPIRY_WARN_DAYS 生成/刷新到期提醒。
3. 维护进程内通知环形缓冲，供界面轮询展示。

测试中可不启动线程，直接调用 process_due_retests() / generate_expiry_reminders()。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from . import config, dates, service
from .schema import connect


class Engine:
    def __init__(self, db_path=None, interval: float = 2.0):
        self.db_path = db_path
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notifications: Deque[dict] = deque(maxlen=100)
        self._lock = threading.Lock()

    # ---------------- 通知 ---------------- #
    def notify(self, kind: str, message: str, payload: dict | None = None) -> None:
        with self._lock:
            self._notifications.append({
                "kind": kind, "message": message, "payload": payload or {},
                "ts": dates.now_str(),
            })

    def drain_notifications(self, after: int = 0) -> list[dict]:
        with self._lock:
            items = list(self._notifications)
        if after >= len(items):
            return []
        # 首次（after=0）最多给最近 20 条，避免积压刷屏
        return items[max(0, after):] if after else items[-20:]

    # ---------------- 静置复测 ---------------- #
    def process_due_retests(self, conn=None) -> int:
        own = conn is None
        conn = conn or connect(self.db_path)
        fired = 0
        try:
            rows = conn.execute(
                """SELECT rm.id AS rid, rm.cylinder_id, rm.batch_id, bp.id AS position_id,
                          b.operator_id, c.serial_no
                   FROM reminders rm
                   JOIN batch_positions bp
                     ON bp.cylinder_id=rm.cylinder_id AND bp.batch_id=rm.batch_id
                   JOIN batches b ON b.id=rm.batch_id
                   JOIN cylinders c ON c.id=rm.cylinder_id
                   WHERE rm.kind='retest' AND rm.status='due'
                     AND datetime(rm.due_at)<=datetime('now','localtime')"""
            ).fetchall()
            for r in rows:
                operator = r["operator_id"] or self._any_operator(conn)
                res = service.run_retest(conn, r["position_id"], operator)
                fired += 1
                if res["leak"]:
                    self.notify("leak",
                                f"复测泄漏：{r['serial_no']} 压降 {res['drop']} MPa，已封存",
                                {"cylinder": r["serial_no"], **res})
                else:
                    self.notify("retest",
                                f"{r['serial_no']} 复测合格，终压 {res['final']} MPa，恢复可领用",
                                {"cylinder": r["serial_no"], **res})
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()
        return fired

    @staticmethod
    def _any_operator(conn) -> int:
        row = conn.execute(
            "SELECT id FROM people WHERE role='operator' AND active=1 ORDER BY id LIMIT 1"
        ).fetchone()
        return row["id"] if row else 0

    # ---------------- 检验到期 ---------------- #
    def generate_expiry_reminders(self, conn=None) -> int:
        own = conn is None
        conn = conn or connect(self.db_path)
        made = 0
        try:
            for c in conn.execute(
                "SELECT id, serial_no, inspection_due, status FROM cylinders "
                "WHERE status!='scrapped'"
            ):
                days = dates.days_until(c["inspection_due"])
                if days <= config.EXPIRY_WARN_DAYS:
                    due_at = f"{c['inspection_due']} 08:00:00"
                    msg = (f"气瓶 {c['serial_no']} 检验已超期 {-days} 天" if days < 0
                           else f"气瓶 {c['serial_no']} 检验将于 {days} 天后到期"
                                f"（{c['inspection_due']}）")
                    cur = conn.execute(
                        """INSERT INTO reminders(kind,cylinder_id,due_at,message)
                           VALUES('expiry',?,?,?)
                           ON CONFLICT(kind,cylinder_id,due_at) DO UPDATE SET message=excluded.message,
                               status=CASE WHEN status='handled' THEN 'handled' ELSE 'due' END""",
                        (c["id"], due_at, msg),
                    )
                    if cur.rowcount and cur.lastrowid:
                        made += 1
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()
        return made

    # ---------------- 线程生命周期 ---------------- #
    def tick(self) -> None:
        conn = connect(self.db_path)
        try:
            self.process_due_retests(conn)
            self.generate_expiry_reminders(conn)
            conn.commit()
        except Exception as e:  # 后台任务不允许拖垮进程
            self.notify("engine_error", f"后台任务异常：{e}")
        finally:
            conn.close()

    def start(self) -> None:
        if self._thread:
            return

        def loop():
            while not self._stop.wait(self.interval):
                self.tick()

        self._thread = threading.Thread(target=loop, name="scba-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
