"""模拟压力台适配器：以可重复、可注入故障的方式提供压力读数。

真实部署时替换为驱动充气柜的硬件适配器，只需实现 read_pressure()。
"""
from __future__ import annotations

import math
from typing import Callable, Optional


class PressureBenchAdapter:
    """进程内模拟压力台。

    - 故障注入：leak=True 时静置读数持续走低；low=True 时充装终压不达标。
    - 读数带可预测的微小扰动（基于序号的确定性伪噪声），避免随机导致测试不稳。
    """

    def __init__(
        self,
        rated: float = 30.0,
        leak: bool = False,
        low: bool = False,
        rng: Callable[[int], float] | None = None,
    ):
        self.rated = rated
        self.leak = leak
        self.low = low
        self._tick = 0
        self._rng = rng or (lambda n: (math.sin(n * 12.9898) * 43758.5453) % 1 - 0.5)

    def _noise(self) -> float:
        self._tick += 1
        return round(self._rng(self._tick) * 0.12, 3)

    # ---- 充装过程 ----
    def initial_reading(self, start_pressure: float) -> float:
        """接上台位时的入口初读。"""
        return round(max(0.0, start_pressure) + self._noise(), 2)

    def fill_target(self) -> float:
        """充装目标终压（故障瓶达不到额定）。"""
        return round(self.rated * (0.82 if self.low else 1.0), 2)

    def final_reading(self, start_pressure: float) -> float:
        """充装完成瞬间的终压读数。"""
        target = self.fill_target()
        return round(min(target, start_pressure + target) if self.low else target + self._noise(), 2)

    # ---- 静置复测 ----
    def settle_reading(self, final_pressure: float, elapsed_s: float) -> float:
        """静置 elapsed_s 秒后的复测读数；泄漏瓶按时间线性掉压。"""
        if self.leak:
            # 约每分钟掉 6 MPa，演示中足以在 12 秒内越过 1 MPa 阈值
            drop = 6.0 * (elapsed_s / 60.0)
            return round(max(0.0, final_pressure - drop + self._noise()), 2)
        return round(final_pressure + self._noise() * 0.3, 2)

    @classmethod
    def for_scenario(cls, scenario: Optional[str], rated: float = 30.0) -> "PressureBenchAdapter":
        """按种子场景构造适配器。"""
        return cls(rated=rated, leak=(scenario == "leak"), low=(scenario == "low"))
