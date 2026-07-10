"""每源令牌桶 + AIMD 单测（无需 PG，注入单调时钟）。"""

from __future__ import annotations

from pyp_server.ratelimit import SourceRateLimiter


def test_token_bucket_per_source() -> None:
    rl = SourceRateLimiter()
    t = 1000.0
    # 额定 2 req/s：桶初始满（=2 个令牌）→ 头两次立取成功，第三次同刻失败
    assert rl.take("s", 2, now=t) is True
    assert rl.take("s", 2, now=t) is True
    assert rl.take("s", 2, now=t) is False
    # 过 0.5s → 补 0.5*2=1 个令牌 → 再取一次成功
    assert rl.take("s", 2, now=t + 0.5) is True
    assert rl.take("s", 2, now=t + 0.5) is False
    # 不同源各自独立
    assert rl.take("other", 2, now=t) is True


def test_rate_limit_zero_means_unlimited() -> None:
    rl = SourceRateLimiter()
    for i in range(100):
        assert rl.take("s", 0, now=1000.0 + i) is True  # 0 = 不限


def test_aimd_decrease_and_recover() -> None:
    rl = SourceRateLimiter(min_rate=0.5, decrease=0.5, increase=1.0)
    t = 0.0
    rl.take("s", 8, now=t)  # 建桶，eff=8
    assert rl.effective_rate("s") == 8
    rl.on_backoff_signal("s")  # 8→4
    assert rl.effective_rate("s") == 4
    rl.on_backoff_signal("s")  # 4→2
    rl.on_backoff_signal("s")  # 2→1
    rl.on_backoff_signal("s")  # 1→0.5（地板）
    rl.on_backoff_signal("s")  # 保持 0.5
    assert rl.effective_rate("s") == 0.5
    for _ in range(20):  # 成功回报加性增，封顶额定 8
        rl.on_ok("s")
    assert rl.effective_rate("s") == 8


def test_backoff_signal_throttles_refill() -> None:
    rl = SourceRateLimiter(min_rate=0.5, decrease=0.5)
    t = 0.0
    # 额定 4：取尽初始 4 个令牌
    for _ in range(4):
        assert rl.take("s", 4, now=t) is True
    assert rl.take("s", 4, now=t) is False
    rl.on_backoff_signal("s")  # eff 4→2
    # 过 1s：按降后 eff=2 补 2 个（而非额定 4）
    assert rl.take("s", 4, now=t + 1.0) is True
    assert rl.take("s", 4, now=t + 1.0) is True
    assert rl.take("s", 4, now=t + 1.0) is False
