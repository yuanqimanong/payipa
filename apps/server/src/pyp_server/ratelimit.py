"""每源令牌桶限流 + AIMD 自动降频（M2）。

进程内、运行态；权威限流在派发环（红线3：所有采集都必须经过统一限流）。每个数据源一个令牌桶，桶容量≈额定速率
（约 1s 突发），按**有效速率** eff 补充。eff 起始=source.rate_limit。当前 M2 切片收到容量限制或访问暂停信号时
先乘性减半（降到 min_rate），成功则加性增（回升到额定）；完整的数据源暂停状态在后续切片接入。rate_limit≤0 视为不限。
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
    blocked_until: float = 0.0  # Retry-After 的进程内快速闸门；权威值同时落 PG


class SourceRateLimiter:
    def __init__(
        self, *, min_rate: float = 0.5, decrease: float = 0.5, increase: float = 1.0, max_buckets: int = 4096
    ) -> None:
        self._b: dict[str, _Bucket] = {}
        self.min_rate = min_rate
        self.decrease = decrease
        self.increase = increase
        # 桶按需创建、原先只增不删（源删除后仍驻留）→ 进程内缓慢泄漏。设容量上界 + LRU 淘汰：
        # 超限时逐出最久未使用且当前未处于冷却窗的桶（被逐出的桶下次使用时按额定速率重建，AIMD 状态归零无害）。
        self.max_buckets = max_buckets

    def _evict_if_needed(self, now: float) -> None:
        overflow = len(self._b) - self.max_buckets
        if overflow <= 0:
            return
        idle = sorted(
            (s for s, b in self._b.items() if b.blocked_until <= now),
            key=lambda s: self._b[s].updated,
        )
        for s in idle[:overflow]:
            del self._b[s]

    def take(self, source: str, base_rate: float, *, now: float | None = None) -> bool:
        """尝试为 source 取一个令牌；成功返回 True 并扣减。base_rate≤0 = 不限。"""
        if base_rate <= 0:
            return True
        now = time.monotonic() if now is None else now
        b = self._b.get(source)
        if b is None:
            self._evict_if_needed(now)
            b = self._b[source] = _Bucket(base=base_rate, eff=base_rate, tokens=base_rate, updated=now)
        b.base = base_rate  # 配置可能被改，跟随
        b.eff = min(b.eff, b.base)  # eff 不超过额定
        # 冷却期间不累计突发令牌；冷却结束后只按结束后的时间补充。
        refill_from = max(b.updated, min(b.blocked_until, now))
        b.tokens = min(b.base, b.tokens + max(0.0, now - refill_from) * b.eff)
        b.updated = now
        if now < b.blocked_until:
            return False
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False

    def on_ok(self, source: str) -> None:
        """成功回报 → 加性增，eff 回升至额定。"""
        b = self._b.get(source)
        if b is not None:
            b.eff = min(b.base, b.eff + self.increase)

    def on_backoff_signal(
        self,
        source: str,
        *,
        retry_after_s: float | None = None,
        now: float | None = None,
    ) -> None:
        """容量限制触发乘性减，并在 Retry-After 窗口内冻结取令牌。"""
        b = self._b.get(source)
        if b is not None:
            b.eff = max(self.min_rate, b.eff * self.decrease)
            b.tokens = min(b.tokens, 1.0)  # 冷却结束最多先放一个探测请求
            if retry_after_s is not None:
                now = time.monotonic() if now is None else now
                b.blocked_until = max(b.blocked_until, now + max(0.0, retry_after_s))

    def effective_rate(self, source: str) -> float | None:
        """当前有效速率（监控/调试用）；未见过的源返回 None。"""
        b = self._b.get(source)
        return b.eff if b is not None else None

    def snapshot(self, source: str, *, now: float | None = None) -> dict[str, float] | None:
        """返回监控所需运行态，不暴露内部 bucket 对象。"""
        b = self._b.get(source)
        if b is None:
            return None
        now = time.monotonic() if now is None else now
        return {
            "base_rate": b.base,
            "effective_rate": b.eff,
            "tokens": max(0.0, b.tokens),
            "retry_in_s": max(0.0, b.blocked_until - now),
        }
