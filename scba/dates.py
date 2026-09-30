"""日期工具（集中处理，避免各处手写格式）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import config

DATE_FMT = "%Y-%m-%d"


def today() -> date:
    return date.today()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_d(s: str) -> date:
    return datetime.strptime(s, DATE_FMT).date()


def add_months(d: date, months: int) -> date:
    """月份进位，处理月末。"""
    month_index = d.year * 12 + (d.month - 1) + months
    y, m = divmod(month_index, 12)
    m += 1
    # 目标月天数不足时取月末
    while True:
        try:
            return date(y, m, d.day)
        except ValueError:
            return date(y, m, 1) - timedelta(days=1)


def due_from(last_inspection: date | str, months: int | None = None) -> str:
    d = last_inspection if isinstance(last_inspection, date) else parse_d(last_inspection)
    return add_months(d, months or config.INSPECTION_VALID_MONTHS).strftime(DATE_FMT)


def days_until(due: str) -> int:
    return (parse_d(due) - today()).days


def settle_due_text(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
