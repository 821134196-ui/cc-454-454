"""集中配置：阈值、路径、角色。数值集中便于测试调参。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SCBA_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "scba.db"
ATTACH_DIR = DATA_DIR / "attachments"

# ---- 压力（单位 MPa，空气呼吸器常规额定 30 MPa）----
RATED_PRESSURE = 30.0
# 归队余压下限：低于该值视为“余压不足/异常耗气”，需说明
RETURN_LOW_PRESSURE = 6.0
# 充装合格终压下限（不低于额定的 95%）
FILL_FINAL_MIN = 28.5
# 静置复测允许的最大压降（MPa），超过判泄漏
RETEST_DROP_LIMIT = 1.0
# 静置复测时长（秒）；演示环境缩短，生产可拉长
RETEST_SETTLE_SECONDS = 12

# ---- 检验有效期（月）：气瓶法定定期检验周期常见 3 年 ----
INSPECTION_VALID_MONTHS = 36
# 到期提醒提前天数
EXPIRY_WARN_DAYS = 60

# ---- 角色 ----
ROLE_FIGHTER = "fighter"        # 战斗员：领用、归队登记
ROLE_OPERATOR = "operator"      # 充装操作员：外观检查、组批、充装、复测
ROLE_SUPERVISOR = "supervisor"  # 装备主管：解除封存、报废确认

ROLE_NAMES = {
    ROLE_FIGHTER: "战斗员",
    ROLE_OPERATOR: "充装操作员",
    ROLE_SUPERVISOR: "装备主管",
}

# 气瓶生命周期状态
CYL_AVAILABLE = "available"          # 可领用
CYL_ISSUED = "issued"                # 出勤中（任务领用未归队）
CYL_RETURNED = "returned_pending"    # 归队待检查
CYL_FILLING = "filling"              # 充装中
CYL_LOANED = "loaned"                # 紧急借调出站
CYL_SEALED = "sealed"                # 封存
CYL_SCRAPPED = "scrapped"            # 报废

CYL_STATUS_NAMES = {
    CYL_AVAILABLE: "可领用",
    CYL_ISSUED: "出勤中",
    CYL_RETURNED: "待检查",
    CYL_FILLING: "充装中",
    CYL_LOANED: "借调出站",
    CYL_SEALED: "封存",
    CYL_SCRAPPED: "报废",
}

# 批次状态
BATCH_OPEN = "open"          # 开放组批
BATCH_RUNNING = "running"    # 充装填机
BATCH_INTERRUPTED = "interrupted"  # 充装中断
BATCH_DONE = "done"          # 充装完成待复测/已复测
BATCH_CLOSED = "closed"      # 已结束（合格瓶回库）

# 充装位置/接口数量
FILL_STATION_PORTS = 4
