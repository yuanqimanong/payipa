"""每源令牌桶限流 + AIMD 自动降频（M2）。

进程内、运行态；权威限流在派发环（红线3：抓取不绕限流）。每个数据源一个令牌桶，桶容量≈额定速率
（约 1s 突发），按**有效速率** eff 补充。eff 起始=source.rate_limit，AIMD：agent 回报封禁(429/503/BLOCKED)
→ 乘性减半（降到 min_rate），成功 → 加性增（回升到额定）。rate_limit≤0 视为不限。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    base: float  # 额定速率 (req/s)
    eff: float  # AIMD 当前有效速率
    tokens: float
    updated: float  # 上次补充的单调时钟


class SourceRateLimiter:
    def __init__(self, *, min_rate: float = 0.5, decrease: float = 0.5, increase: float = 1.0) -> None:
        self._b: dict[str, _Bucket] = {}
        self.min_rate = min_rate
        self.decrease = decrease
        self.increase = increase

    def take(self, source: str, base_rate: float, *, now: float | None = None) -> bool:
        """尝试为 source 取一个令牌；成功返回 True 并扣减。base_rate≤0 = 不限。"""
        if base_rate <= 0:
            return True
        now = time.monotonic() if now is None else now
        b = self._b.get(source)
        if b is None:
            b = self._b[source] = _Bucket(base=base_rate, eff=base_rate, tokens=base_rate, updated=now)
        b.base = base_rate  # 配置可能被改，跟随
        b.eff = min(b.eff, b.base)  # eff 不超过额定
        b.tokens = min(b.base, b.tokens + max(0.0, now - b.updated) * b.eff)
        b.updated = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False

    def on_ok(self, source: str) -> None:
        """成功回报 → 加性增，eff 回升至额定。"""
        b = self._b.get(source)
        if b is not None:
            b.eff = min(b.base, b.eff + self.increase)

    def on_blocked(self, source: str) -> None:
        """封禁回报（BLOCKED/429/503）→ 乘性减，eff 降到 min_rate 地板。"""
        b = self._b.get(source)
        if b is not None:
            b.eff = max(self.min_rate, b.eff * self.decrease)

    def effective_rate(self, source: str) -> float | None:
        """当前有效速率（监控/调试用）；未见过的源返回 None。"""
        b = self._b.get(source)
        return b.eff if b is not None else None
