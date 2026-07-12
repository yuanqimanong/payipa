"""单测（无需 PG）：登录失败节流 LoginThrottle —— 达阈值上锁、时间到解锁、成功清零、按 key 隔离。"""

from __future__ import annotations

from pyp_server.login_guard import LoginThrottle


def test_locks_after_threshold_and_unlocks_after_window() -> None:
    t = LoginThrottle(max_failures=3, window_s=100.0, lockout_s=60.0)
    k = t.key("1.2.3.4", "alice")
    assert t.retry_after(k, now=0) == 0
    t.record_failure(k, now=0)
    t.record_failure(k, now=1)
    assert t.retry_after(k, now=2) == 0  # 未达阈值
    t.record_failure(k, now=2)  # 第 3 次 → 上锁
    assert t.retry_after(k, now=2) > 0
    assert t.retry_after(k, now=61) > 0  # 锁定窗内仍拒
    assert t.retry_after(k, now=63) == 0  # 60s 后解锁


def test_success_clears_and_keys_isolated() -> None:
    t = LoginThrottle(max_failures=2, window_s=100.0, lockout_s=60.0)
    ka, kb = t.key("1.1.1.1", "alice"), t.key("1.1.1.1", "bob")
    t.record_failure(ka, now=0)
    t.clear(ka)  # 成功登录清零
    t.record_failure(ka, now=1)
    assert t.retry_after(ka, now=1) == 0  # 计数已被清零，未上锁
    # 不同 (ip, username) 互不影响：bob 的失败不锁 alice
    t.record_failure(kb, now=0)
    t.record_failure(kb, now=1)
    assert t.retry_after(kb, now=1) > 0
    assert t.retry_after(ka, now=1) == 0


def test_stale_failures_fall_out_of_window() -> None:
    t = LoginThrottle(max_failures=3, window_s=10.0, lockout_s=60.0)
    k = t.key("9.9.9.9", "carol")
    t.record_failure(k, now=0)
    t.record_failure(k, now=5)
    t.record_failure(k, now=100)  # 前两次已滑出 10s 窗口 → 本次是窗口内第 1 次，不上锁
    assert t.retry_after(k, now=100) == 0
