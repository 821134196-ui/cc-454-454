"""领域常量：角色、气瓶状态、阈值与业务错误。"""

# ---------- 角色 ----------
ROLE_FIREFIGHTER = "FIREFIGHTER"   # 战斗员（可扫码归队）
ROLE_DISPATCHER = "DISPATCHER"     # 装备员/充装员
ROLE_INSPECTOR = "INSPECTOR"       # 检验员
ROLE_SUPERVISOR = "SUPERVISOR"     # 装备主管（解封、报废确认）

ROLE_LABELS = {
    ROLE_FIREFIGHTER: "战斗员",
    ROLE_DISPATCHER: "装备员",
    ROLE_INSPECTOR: "检验员",
    ROLE_SUPERVISOR: "装备主管",
}
OPS_ROLES = (ROLE_DISPATCHER, ROLE_INSPECTOR, ROLE_SUPERVISOR)

# ---------- 气瓶状态 ----------
ST_AVAILABLE = "AVAILABLE"                    # 可领用
ST_ISSUED = "ISSUED"                          # 已领用（含紧急借调）
ST_PENDING_INSPECTION = "PENDING_INSPECTION"  # 归队待检查
ST_PENDING_FILL = "PENDING_FILL"              # 检查合格、待充装
ST_IN_BATCH = "IN_BATCH"                      # 已加入充装批次
ST_FILLING = "FILLING"                        # 充装中
ST_SETTLING = "SETTLING"                      # 静置待复测
ST_PENDING_PAIR = "PENDING_PAIR"              # 复测合格、待配套核验
ST_REPAIR = "REPAIR"                          # 维修中
ST_SEALED = "SEALED"                          # 封存（含待报废审议）
ST_SCRAPPED = "SCRAPPED"                      # 报废

STATUS_LABELS = {
    ST_AVAILABLE: "可领用",
    ST_ISSUED: "已领用",
    ST_PENDING_INSPECTION: "待检查",
    ST_PENDING_FILL: "待充装",
    ST_IN_BATCH: "已入批",
    ST_FILLING: "充装中",
    ST_SETTLING: "静置中",
    ST_PENDING_PAIR: "待配套",
    ST_REPAIR: "维修中",
    ST_SEALED: "封存",
    ST_SCRAPPED: "报废",
}

# 归队扫码后、尚未回到可领用之前的状态：重复扫码只记审计事件
POST_RETURN_STATES = {
    ST_PENDING_INSPECTION, ST_PENDING_FILL, ST_IN_BATCH,
    ST_FILLING, ST_SETTLING, ST_PENDING_PAIR,
}

# ---------- 批次/接口/领用 ----------
BATCH_OPEN = "OPEN"            # 组批中
BATCH_CHARGING = "CHARGING"    # 充装中
BATCH_SETTLING = "SETTLING"    # 静置中（未结束）
BATCH_COMPLETED = "COMPLETED"
BATCH_ABORTED = "ABORTED"
BATCH_OPEN_STATES = {BATCH_OPEN, BATCH_CHARGING, BATCH_SETTLING}

PORT_WAITING = "WAITING"
PORT_FILLING = "FILLING"
PORT_FILLED_OK = "FILLED_OK"
PORT_FILL_LOW = "FILL_LOW"
PORT_RETEST_PASS = "RETEST_PASS"
PORT_RETEST_LEAK = "RETEST_LEAK"

ISSUE_TASK = "TASK"
ISSUE_EMERGENCY_LOAN = "EMERGENCY_LOAN"
ISSUE_TYPE_LABELS = {ISSUE_TASK: "任务领用", ISSUE_EMERGENCY_LOAN: "紧急借调"}

# ---------- 压力与检验阈值 ----------
RATED_PRESSURE_MPA = 30.0
FINAL_MIN_RATIO = 0.95                  # 终压不得低于额定压力 95%（28.5 MPa）
FINAL_MIN_MPA = round(RATED_PRESSURE_MPA * FINAL_MIN_RATIO, 1)
SETTLE_DROP_MAX_MPA = 1.0               # 静置复测压降不超过 1.0 MPa
INSPECTION_INTERVAL_MONTHS = 36         # 碳纤维全缠绕气瓶水压试验周期 3 年
INSPECTION_DUE_SOON_DAYS = 30

# 故障注入（模拟适配器）
FAULT_LOW_FILL = "LOW_FILL"
FAULT_LEAK = "LEAK"

# ---------- 处置类型 ----------
DISP = {
    "VALVE_DAMAGE": "瓶阀损伤",
    "APPEARANCE_FAIL": "外观不合格",
    "OVERDUE": "超期未检",
    "FILL_LOW": "终压不足",
    "RETEST_LEAK": "复测泄漏",
    "PAIR_MISMATCH": "错误配套",
    "FILL_INTERRUPTED": "充装中断",
    "EMERGENCY_LOAN": "紧急借调",
}


class DomainError(Exception):
    """业务规则违例。code 供前端/测试精确断言。"""

    def __init__(self, code, message, http=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


class AuthError(DomainError):
    def __init__(self, message="人员不存在或身份无效"):
        super().__init__("AUTH_INVALID", message, http=401)


class PermissionError(DomainError):  # noqa: A001 - 业务权限，非内建场景冲突
    def __init__(self, message="无权执行该操作"):
        super().__init__("FORBIDDEN", message, http=403)
