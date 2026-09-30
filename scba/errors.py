"""领域异常与处置分类。"""
from __future__ import annotations


class SCBAError(Exception):
    """业务规则冲突（HTTP 400）。"""

    code = "business_rule"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotFound(SCBAError):
    code = "not_found"


class PermissionDenied(SCBAError):
    code = "permission_denied"


class Conflict(SCBAError):
    code = "conflict"


# 七类异常处置类型（瓶阀损伤/超期未检/终压不足/复测泄漏/错误配套/充装中断/紧急借调）
DISPOSAL_LABELS = {
    "valve_damage": "瓶阀损伤",
    "overdue": "超期未检",
    "low_final": "终压不足",
    "leak": "复测泄漏",
    "mismatch": "错误配套",
    "interrupt": "充装中断",
    "loan": "紧急借调",
}
