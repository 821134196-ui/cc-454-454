"""训练数据与启动样例（幂等：仅空库执行）。

样例覆盖：泄漏封存、终压不足维修、紧急借调、报废确认、配套错误后纠正、
超期未检、到期临近、外观待检、维修中、主管封存等全部处置分支。
"""
import datetime as dt
import os

from . import domain as D

PERSONNEL = [
    ("FF001", "张伟", D.ROLE_FIREFIGHTER, "特勤消防救援一站"),
    ("FF002", "李娜", D.ROLE_FIREFIGHTER, "特勤消防救援一站"),
    ("FF003", "王强", D.ROLE_FIREFIGHTER, "莲湖路消防救援站"),
    ("FF004", "周洋", D.ROLE_FIREFIGHTER, "莲湖路消防救援站"),
    ("DP001", "赵磊", D.ROLE_DISPATCHER, "特勤消防救援一站"),
    ("DP002", "吴敏", D.ROLE_DISPATCHER, "装备库房"),
    ("IN001", "钱进", D.ROLE_INSPECTOR, "气瓶检验站"),
    ("SUP01", "孙明", D.ROLE_SUPERVISOR, "战训装备股"),
]

TASKS = [
    ("T-2026-001", "高层建筑灭火实战演练", "灭火", "金融中心 A 座", "2026-09-30 09:00"),
    ("T-2026-002", "地下空间搜救训练", "搜救", "轨道交通 3 号线训练场", "2026-09-30 14:00"),
    ("T-2026-003", "化工装置堵漏演训", "堵漏", "化工园区训练基地", "2026-10-02 08:30"),
    ("T-2026-004", "辖区六熟悉巡防", "巡防", "辖区重点单位", "2026-10-03 15:00"),
]

# (编号, 背架, 上次检测, 容积)  有效期=上次检测+36 个月
CYLINDERS = [
    ("SCBA-0601", "JZ-0601", "2024-05-20", 6.8),
    ("SCBA-0602", "JZ-0602", "2024-07-11", 6.8),
    ("SCBA-0603", "JZ-0603", "2024-08-02", 6.8),
    ("SCBA-0604", "JZ-0604", "2024-09-15", 6.8),
    ("SCBA-0605", "JZ-0605", "2025-01-08", 6.8),
    ("SCBA-0606", "JZ-0606", "2025-02-19", 6.8),
    ("SCBA-0607", "JZ-0607", "2023-06-30", 6.8),
    ("SCBA-0608", "JZ-0608", "2024-10-25", 6.8),
    ("SCBA-0609", "JZ-0609", "2024-11-30", 6.8),
    ("SCBA-0610", "JZ-0610", "2025-03-12", 6.8),
    ("SCBA-0611", "JZ-0611", "2025-04-18", 6.8),
    ("SCBA-0612", "JZ-0612", "2022-08-01", 6.8),   # 已超期（2025-08-01 到期）
    ("SCBA-0613", "JZ-0613", "2023-10-15", 6.8),   # 2026-10-15 到期，临近
    ("SCBA-0614", "JZ-0614", "2023-10-28", 6.8),   # 2026-10-28 到期，临近
    ("SCBA-0615", "JZ-0615", "2025-05-06", 6.8),
    ("SCBA-0616", "JZ-0616", "2024-03-22", 9.0),
]


def _has_marker(conn, marker):
    return conn.execute("SELECT 1 FROM events WHERE type=? LIMIT 1", (marker,)).fetchone()


def seed(conn, svc, adapter, attachments_dir):
    if _has_marker(conn, "SEED_BASE"):
        return False
    import time

    # ---------- 人员 / 任务 ----------
    for pid, name, role, unit in PERSONNEL:
        conn.execute(
            "INSERT INTO personnel(person_id,name,role,unit,active) VALUES(?,?,?,?,1)",
            (pid, name, role, unit))
    for tid, name, ttype, loc, planned in TASKS:
        conn.execute(
            "INSERT INTO tasks(task_id,name,task_type,location,planned_at) VALUES(?,?,?,?,?)",
            (tid, name, ttype, loc, planned))

    # ---------- 气瓶台账 ----------
    from .service import add_months, parse_date
    now = time.time()
    for no, frame, last, vol in CYLINDERS:
        due = add_months(parse_date(last), D.INSPECTION_INTERVAL_MONTHS).isoformat()
        conn.execute(
            """INSERT INTO cylinders(cyl_no,frame_no,rated_pressure,volume_l,material,
               last_inspection,inspection_due,status,current_pressure,created_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (no, frame, D.RATED_PRESSURE_MPA, vol, "碳纤维全缠绕",
             last, due, D.ST_AVAILABLE, 30.0, now))
    conn.commit()
    db_append(svc, "SEED_BASE", payload={"personnel": len(PERSONNEL),
                                         "cylinders": len(CYLINDERS)})

    # ============ 样例流程 ============
    # 1) 泄漏 + 终压不足 + 正常复测 的充装批次
    adapter.inject_fault("SCBA-0602", D.FAULT_LEAK)
    adapter.inject_fault("SCBA-0604", D.FAULT_LOW_FILL)
    svc.issue("SCBA-0602", "FF001", task_id="T-2026-002", actor_id="DP001")
    svc.return_cylinder("SCBA-0602", "FF001", 18.2)
    chk_0602 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0602' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0602, "DP001", True, True, "归队外观无异常")

    svc.issue("SCBA-0603", "FF002", task_id="T-2026-001", actor_id="DP001")
    svc.return_cylinder("SCBA-0603", "FF002", 23.0)
    chk_0603 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0603' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0603, "DP001", True, True, "瓶阀旋合顺畅")

    svc.issue("SCBA-0604", "FF003", task_id="T-2026-003", actor_id="DP001")
    svc.return_cylinder("SCBA-0604", "FF003", 12.5)
    chk_0604 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0604' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0604, "DP001", True, True, "外观合格")

    svc.issue("SCBA-0605", "FF004", task_id="T-2026-004", actor_id="DP001")
    svc.return_cylinder("SCBA-0605", "FF004", 20.0)
    chk_0605 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0605' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0605, "DP001", True, True, None)

    bid = svc.create_batch("DP001", positions=4)
    svc.assign_port(bid, 1, "SCBA-0602", "DP001")
    svc.assign_port(bid, 2, "SCBA-0603", "DP001")
    svc.assign_port(bid, 3, "SCBA-0604", "DP001")
    svc.assign_port(bid, 4, "SCBA-0605", "DP001")
    svc.start_charging(bid, "DP001")
    svc.complete_charging(bid, "DP001")  # 0604 终压不足转维修，其余静置
    port_leak = conn.execute(
        "SELECT id FROM batch_ports WHERE batch_id=? AND cyl_no='SCBA-0602'", (bid,)).fetchone()["id"]
    port_ok = conn.execute(
        "SELECT id FROM batch_ports WHERE batch_id=? AND cyl_no='SCBA-0603'", (bid,)).fetchone()["id"]
    svc.retest_port(port_leak, "DP001", force=True)   # 泄漏 -> 封存待判
    svc.retest_port(port_ok, "DP001", force=True)     # 合格 -> 待配套
    # 配套：先错扫（错误配套留痕），再核对正确编号回到可领用
    try:
        svc.pair_check("SCBA-0603", "JZ-9999", "FF002")
    except D.DomainError:
        pass
    svc.pair_check("SCBA-0603", "JZ-0603", "DP001")
    # 0604 终压不足排修后恢复待充装（故障注入清除，下轮可正常充装）
    svc.repair_done("SCBA-0604", "DP001", "更换密封件后复核")
    adapter.clear_fault("SCBA-0604")
    adapter.clear_fault("SCBA-0602")
    # 0605 保持静置中（约 15 分钟后由后台任务提醒复测）

    # 2) 紧急借调（外站）
    svc.issue("SCBA-0606", "FF002", issue_type=D.ISSUE_EMERGENCY_LOAN,
              target_unit="解放街消防救援站", actor_id="SUP01")

    # 3) 瓶阀损伤报废气瓶：装备员提报废审议，装备主管确认报废
    svc.propose_condemn("SCBA-0607", "DP001", "瓶阀螺纹滑牙、瓶颈划伤超标，无维修价值")
    svc.confirm_scrap("SCBA-0607", "SUP01", "报废评审通过，登记销账")

    # 4) 归队待检查样例（带本地检测附件）
    svc.issue("SCBA-0608", "FF003", task_id="T-2026-003", actor_id="DP001")
    svc.return_cylinder("SCBA-0608", "FF003", 21.5)
    chk_0608 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0608' ORDER BY id LIMIT 1").fetchone()["id"]
    _write_sample_attachment(conn, svc, "SCBA-0608", chk_0608, attachments_dir)

    # 5) 瓶阀损伤 -> 维修中
    svc.issue("SCBA-0610", "FF004", task_id="T-2026-002", actor_id="DP001")
    svc.return_cylinder("SCBA-0610", "FF004", 19.0)
    chk_0610 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0610' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0610, "DP001", True, False, "瓶阀手轮转不到位，存在卡滞")

    # 6) 普通任务领用在途
    svc.issue("SCBA-0615", "FF001", task_id="T-2026-001", actor_id="DP001")

    # 7) 主管封存（外观深划伤待评估）
    svc.seal("SCBA-0616", "SUP01", "瓶体发现深划伤，待检验站评估")

    # 0609 作为检查合格待充装的空闲瓶
    svc.issue("SCBA-0609", "FF002", task_id="T-2026-004", actor_id="DP001")
    svc.return_cylinder("SCBA-0609", "FF002", 25.0)
    chk_0609 = conn.execute(
        "SELECT id FROM return_checks WHERE cyl_no='SCBA-0609' ORDER BY id LIMIT 1").fetchone()["id"]
    svc.inspect_return(chk_0609, "DP001", True, True, None)

    conn.commit()
    svc.scan_reminders()
    return True


def db_append(svc, etype, payload=None):
    from .db import append_event
    append_event(svc.conn, etype, None, "SYSTEM", None, None, payload)
    svc.conn.commit()


def _write_sample_attachment(conn, svc, cyl_no, check_id, attachments_dir):
    os.makedirs(attachments_dir, exist_ok=True)
    fname = f"{cyl_no}_外观检查记录.txt"
    stored = os.path.join(attachments_dir, fname)
    with open(stored, "w", encoding="utf-8") as f:
        f.write(
            "空气呼吸器气瓶归队外观检查记录\n"
            f"气瓶编号：{cyl_no}\n"
            f"检查单：RC-{check_id}\n"
            "检查项：瓶体划伤、缠绕层、瓶阀、压力表、颈圈\n"
            "备注：待检查员现场判定，附件为扫码拍摄占位记录。\n")
    cur = conn.execute(
        """INSERT INTO attachments(cyl_no,filename,stored_path,content_type,actor_id,ts)
           VALUES(?,?,?,?,?,strftime('%s','now'))""",
        (cyl_no, fname, stored, "text/plain", "DP001"))
    conn.execute("UPDATE return_checks SET attachment_id=? WHERE id=?",
                 (cur.lastrowid, check_id))
