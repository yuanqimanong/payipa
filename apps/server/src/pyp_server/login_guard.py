"""登录失败节流：进程内、按 (客户端 IP, 用户名) 计数，抵御在线暴力破解 / 撞库。

单 worker 下进程内状态即够（P0-09）；反向代理场景须先经 ProxyHeaders 拿到真实客户端 IP（见 docs/09 部署）。
按 (ip, username) 而非仅 username：避免攻击者用错误尝试把某账号全局锁死（DoS），同时仍拦截针对单账号的连续猜测。
密码哈希用 argon2（慢）已有一定缓解，但缺在线节流仍是可对外暴露管理台的安全基线缺口。
"""

from __future__ import annotations

import time
from collections import deque


class LoginThrottle:
    """滑动窗口失败计数 + 锁定窗。窗口内失败达 max_failures → 锁定 lockout_s 秒，其间一律拒绝。"""

    def __init__(
        self, *, max_failures: int = 5, window_s: float = 300.0, lockout_s: float = 300.0, max_keys: int = 8192
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self.max_keys = max_keys
        self._fails: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def key(ip: str | None, username: str) -> str:
        return f"{ip or '?'}::{username}"

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """当前 key 的剩余锁定秒数（0 = 未锁定，可尝试）。"""
        now = time.monotonic() if now is None else now
        return max(0.0, self._locked_until.get(key, 0.0) - now)

    def record_failure(self, key: str, *, now: float | None = None) -> float:
        """记一次失败；窗口内累计达阈值即上锁。返回上锁后的剩余秒数（未上锁为 0）。"""
        now = time.monotonic() if now is None else now
        if len(self._fails) + len(self._locked_until) > self.max_keys:  # 容量护栏，防进程内无界增长
            self._prune(now)
        dq = self._fails.setdefault(key, deque())
        cutoff = now - self.window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        if len(dq) >= self.max_failures:
            self._locked_until[key] = now + self.lockout_s
            dq.clear()
        return self.retry_after(key, now=now)

    def clear(self, key: str) -> None:
        """登录成功：清除该 key 的失败计数与锁定。"""
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)

    def reset(self) -> None:
        self._fails.clear()
        self._locked_until.clear()

    def _prune(self, now: float) -> None:
        for k, until in list(self._locked_until.items()):
            if until <= now:
                self._locked_until.pop(k, None)
        cutoff = now - self.window_s
        for k, dq in list(self._fails.items()):
            if not dq or dq[-1] < cutoff:
                self._fails.pop(k, None)
