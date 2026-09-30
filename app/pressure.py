"""模拟压力台适配器：给出充装终压与静置复测读数，支持故障注入。

读数确定性可复现（不使用随机数），便于测试与样例展示：
- 正常气瓶充装到额定 30.0 MPa；
- LOW_FILL：充装无法建压，终压 26.3 MPa（终压不足）；
- LEAK：充装正常，但静置后明显掉压（复测泄漏）。
"""
from . import domain as D


class MockPressureAdapter:
    NAME = "模拟充装压力台 RCD-MK1"

    def __init__(self):
        self._faults = {}  # cyl_no -> FAULT_*

    def inject_fault(self, cyl_no, kind):
        assert kind in (D.FAULT_LOW_FILL, D.FAULT_LEAK)
        self._faults[cyl_no] = kind

    def clear_fault(self, cyl_no):
        self._faults.pop(cyl_no, None)

    def fault_of(self, cyl_no):
        return self._faults.get(cyl_no)

    def read_fill_pressure(self, cyl_no, rated):
        """充装完成瞬间的终压读数。"""
        if self._faults.get(cyl_no) == D.FAULT_LOW_FILL:
            return 26.3
        # 正常：按编号尾号产生 ±0.1 的确定性微小偏差，钳制在合理区间
        tail = int("".join(ch for ch in str(cyl_no) if ch.isdigit())[-2:])
        delta = (tail % 3 - 1) * 0.1
        return round(min(rated + 0.3, max(rated - 0.2, rated + delta)), 1)

    def read_settle_pressure(self, cyl_no, initial):
        """静置期满后的复测读数。"""
        if self._faults.get(cyl_no) == D.FAULT_LEAK:
            return round(initial - 3.5, 1)
        tail = int("".join(ch for ch in str(cyl_no) if ch.isdigit())[-2:])
        drop = 0.1 + (tail % 3) * 0.1  # 0.1 / 0.2 / 0.3 MPa
        return round(initial - drop, 1)
