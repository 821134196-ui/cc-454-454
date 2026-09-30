"""训练数据与演示样例种子。

启动时若库为空则写入：
- 人员（战斗员/充装操作员/装备主管）、背架、训练任务；
- 常规气瓶台账；
- 一个进行中的充装批次（含正常/泄漏/终压不足接口），静置复测由进程内引擎自动触发；
- 历史复测泄漏、紧急借调出站、瓶阀报废、超期未检等处置样例；
- 本地检测附件（文本）。
"""
from __future__ import annotations

from pathlib import Path

from . import config, dates, service
from .schema import connect

# (编号, 背架码, 额定, 出厂, 上次检测)
PEOPLE = [
    ("王磊", "XF1001", "fighter", "特勤一班"),
    ("李航", "XF1002", "fighter", "特勤一班"),
    ("赵鹏", "XF1003", "fighter", "特勤二班"),
    ("陈涛", "XF1004", "fighter", "特勤二班"),
    ("周勇", "CZ2001", "operator", "充气室"),
    ("孙建国", "ZG3001", "supervisor", "装备管理组"),
]

CYLINDERS = [
    # serial, harness, rated, manufacture, last_inspection, pressure
    ("QP-6801", "BJ-A01", 30.0, "2021-05-08", "2025-04-12", 30.0),
    ("QP-6802", "BJ-A02", 30.0, "2021-06-20", "2025-04-12", 29.6),
    ("QP-6803", "BJ-A03", 30.0, "2022-01-15", "2025-05-06", 30.0),
    ("QP-6804", "BJ-A04", 30.0, "2022-03-02", "2025-05-06", 28.9),
    ("QP-6805", "BJ-A05", 30.0, "2022-07-19", "2025-06-10", 30.0),
    ("QP-6806", "BJ-A06", 30.0, "2021-11-11", "2025-06-10", 24.0),   # 出勤中
    ("QP-6807", "BJ-A07", 30.0, "2022-09-25", "2025-09-01", 8.5),    # 归队待检查
    ("QP-6808", None,    30.0, "2023-02-14", "2025-09-01", 12.0),    # 入充批次（正常）
    ("QP-6809", None,    30.0, "2023-02-14", "2025-09-01", 11.0),    # 入充批次（正常）
    ("QP-6810", None,    30.0, "2023-04-01", "2025-09-01", 9.0),     # 入充批次（泄漏）
    ("QP-6811", None,    30.0, "2023-04-01", "2025-09-01", 7.5),     # 入充批次（终压不足）
    ("QP-6812", "BJ-A08", 30.0, "2022-12-30", "2025-08-20", 30.0),   # 紧急借调
    ("QP-6813", "BJ-A09", 30.0, "2020-11-05", "2023-11-20", 30.0),   # 检验即将到期
    ("QP-6814", "BJ-A10", 30.0, "2020-08-18", "2023-03-01", 18.0),   # 超期未检封存
    ("QP-6815", None,    30.0, "2021-02-22", "2025-07-07", 26.0),    # 历史泄漏样例
    ("QP-6816", None,    30.0, "2020-05-30", "2025-03-15", 20.0),    # 瓶阀损伤报废
]

TASKS = [
    ("RW-0930-01", "高层灭火实战训练", "训练塔A", "09:00", "ongoing"),
    ("RW-0930-02", "浓烟环境搜救训练", "烟热训练室", "14:00", "planned"),
    ("RW-0929-01", "空气呼吸器佩戴专项", "西操场", "16:00", "closed"),
]


def _id(conn, table: str, col: str, val) -> int:
    return conn.execute(f"SELECT id FROM {table} WHERE {col}=?", (val,)).fetchone()["id"]


def _write_attachment(conn, serial: str, cid: int, kind: str, content: str,
                      note: str) -> int:
    config.ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{serial}_{kind}.txt"
    path = config.ATTACH_DIR / fname
    path.write_text(content, encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO attachments(cylinder_id,kind,filename,relpath,note) VALUES(?,?,?,?,?)",
        (cid, kind, fname, f"attachments/{fname}", note),
    )
    return cur.lastrowid


def _return_inspect(conn, serial: str, pressure: float, fighter: int, operator: int,
                    *, appearance=True, valve=True, findings="", attach: int | None = None):
    cid = _id(conn, "cylinders", "serial_no", serial)
    r = service.return_cylinder(conn, cid, pressure, fighter, note="训练归队")
    rid = r["return_id"]
    res = service.inspect_appearance(
        conn, rid, appearance_ok=appearance, valve_ok=valve, findings=findings,
        inspector_id=operator, attachment_id=attach)
    return cid, res


def seeded(db_path=None) -> bool:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"] > 0
    finally:
        conn.close()


def seed(db_path=None) -> bool:
    conn = connect(db_path)
    try:
        if conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"]:
            return False

        # ---- 人员 ----
        for name, badge, role, squad in PEOPLE:
            conn.execute("INSERT INTO people(name,badge,role,squad) VALUES(?,?,?,?)",
                         (name, badge, role, squad))
        fid1 = _id(conn, "people", "badge", "XF1001")  # 王磊
        fid2 = _id(conn, "people", "badge", "XF1002")  # 李航
        operator = _id(conn, "people", "badge", "CZ2001")  # 周勇
        supervisor = _id(conn, "people", "badge", "ZG3001")  # 孙建国
        conn.commit()

        # ---- 背架 ----
        harnesses = sorted({h for _, h, *_ in CYLINDERS if h})
        for i, code in enumerate(harnesses, 1):
            conn.execute("INSERT INTO harnesses(code,model,status) VALUES(?,?, 'active')",
                         (code, "RHZK6.8/A"))
        conn.commit()

        # ---- 气瓶 ----
        for serial, hcode, rated, mfg, last, pressure in CYLINDERS:
            hid = _id(conn, "harnesses", "code", hcode) if hcode else None
            conn.execute(
                """INSERT INTO cylinders(serial_no,harness_id,rated_pressure,manufacture_date,
                   last_inspection_date,inspection_due,current_pressure,status,paired_confirmed_at)
                   VALUES(?,?,?,?,?,?,?, 'available', datetime('now','localtime'))""",
                (serial, hid, rated, mfg, last, dates.due_from(last), pressure),
            )
        conn.commit()

        # ---- 训练任务 ----
        for code, name, loc, hm, status in TASKS:
            conn.execute(
                """INSERT INTO tasks(code,name,kind,location,scheduled_at,status)
                   VALUES(?,?, '训练',?, datetime('now','localtime'),?)""",
                (code, name, loc, status),
            )
        conn.commit()

        # 1) 出勤中：王磊领用 QP-6806 参加高层训练
        task1 = _id(conn, "tasks", "code", "RW-0930-01")
        service.issue_cylinder(conn, _id(conn, "cylinders", "serial_no", "QP-6806"),
                               task1, fid1)
        # 李航领用 QP-6803 参加下午搜救训练（planned 任务）
        task2 = _id(conn, "tasks", "code", "RW-0930-02")
        service.issue_cylinder(conn, _id(conn, "cylinders", "serial_no", "QP-6803"),
                               task2, fid2)

        # 2) 归队待检查：QP-6807（余压偏低，待外观检查）
        service.return_cylinder(
            conn, _id(conn, "cylinders", "serial_no", "QP-6807"), 8.5, fid1,
            note="高层训练归队，余压偏低")

        # 3) 进行中的充装批次：6808/6809 正常，6810 泄漏，6811 终压不足
        for s in ("QP-6808", "QP-6809", "QP-6810", "QP-6811"):
            _return_inspect(conn, s, 10.0, fid2, operator)
        b = service.create_batch(conn, operator)
        bid = b["batch_id"]
        # 覆盖批次编码，便于演示识别
        conn.execute("UPDATE batches SET code=? WHERE id=?", (f"PB-演示-{bid:03d}", bid))
        conn.commit()
        for s, scenario in (("QP-6808", ""), ("QP-6809", ""),
                            ("QP-6810", "leak"), ("QP-6811", "low")):
            service.add_to_batch(conn, bid, _id(conn, "cylinders", "serial_no", s),
                                 scenario=scenario)
        service.start_fill(conn, bid, operator)
        service.complete_fill(conn, bid, operator)
        # 6810 复测由后台引擎在静置到期后自动执行（约 12 秒，界面会推送泄漏通知）

        # 4) 历史复测泄漏样例：QP-6815（已完成封存，供压力复测/台账视图展示）
        cid15, _ = _return_inspect(conn, "QP-6815", 26.0, fid1, operator)
        att15 = None
        b2 = service.create_batch(conn, operator)
        bid2 = b2["batch_id"]
        conn.execute("UPDATE batches SET code=? WHERE id=?", (f"PB-泄漏样例-{bid2:03d}", bid2))
        conn.commit()
        service.add_to_batch(conn, bid2, cid15, scenario="leak")
        service.start_fill(conn, bid2, operator)
        service.complete_fill(conn, bid2, operator)
        pos = conn.execute(
            "SELECT id FROM batch_positions WHERE batch_id=? AND cylinder_id=?", (bid2, cid15)
        ).fetchone()
        service.run_retest(conn, pos["id"], operator, elapsed_s=60)
        att15 = _write_attachment(
            conn, "QP-6815", cid15, "leak",
            "静置复测记录：初压 30.0 MPa，60 秒后 24.0 MPa，压降 6.0 MPa，判定泄漏，封存待查。\n",
            "复测泄漏检测单")

        # 5) 超期未检样例：QP-6814 归队检查时判定超期，封存
        cid14 = _id(conn, "cylinders", "serial_no", "QP-6814")
        r14 = service.return_cylinder(conn, cid14, 18.0, fid2, note="例行归队")
        att14 = _write_attachment(
            conn, "QP-6814", cid14, "appearance",
            "外观检查记录：瓶体外观无异常，瓶阀无损伤；检验有效期 2026-03-01 已超期。\n",
            "超期未检核查单")
        service.inspect_appearance(conn, r14["return_id"], appearance_ok=True, valve_ok=True,
                                   findings="检验超期", inspector_id=operator,
                                   attachment_id=att14)

        # 6) 瓶阀损伤 → 装备主管确认报废：QP-6816
        cid16 = _id(conn, "cylinders", "serial_no", "QP-6816")
        r16 = service.return_cylinder(conn, cid16, 20.0, fid1, note="归队发现瓶阀碰伤")
        att16 = _write_attachment(
            conn, "QP-6816", cid16, "appearance",
            "外观检查记录：瓶阀手轮崩缺、阀体有磕碰变形痕迹，启闭卡滞，判定瓶阀损伤。\n",
            "瓶阀损伤检查单")
        service.inspect_appearance(conn, r16["return_id"], appearance_ok=True, valve_ok=False,
                                   findings="瓶阀手轮崩缺，阀体变形", inspector_id=operator,
                                   attachment_id=att16)
        service.confirm_scrap(conn, cid16, supervisor, "瓶阀损伤不可修复，装备主管确认报废")

        # 7) 紧急借调样例：QP-6812 借往兄弟单位（未归还）
        service.emergency_loan(
            conn, _id(conn, "cylinders", "serial_no", "QP-6812"),
            "特勤二站（跨队增援）", supervisor)

        conn.commit()
        return True
    finally:
        conn.close()
