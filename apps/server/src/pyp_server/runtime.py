"""后台环运行状态 + 单实例锁（P0-06/P0-09）。

每个后台环（派发/推送）每 tick 打点到 ``app.state.loop_health``，/readyz 据此判断
「循环还活着」而不是只看进程存活。单实例锁用 PG advisory lock：同一 pyp 库上
只允许一个进程持有后台环（v1 明确单实例单 worker，多 worker 会分裂 Hub/限流状态）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 单实例锁 key（任意固定 64 位整数；同库互斥）
LOCK_KEY = 7473970121


@dataclass
class LoopHealth:
    """一个后台环的心跳档案：最近成功 tick、连续失败、最后错误。"""

    name: str
    interval_s: float
    started_at: float = field(default_factory=time.monotonic)
    last_ok_at: float | None = None
    consecutive_fails: int = 0
    last_error: str | None = None

    def ok(self) -> None:
        self.last_ok_at = time.monotonic()
        self.consecutive_fails = 0
        self.last_error = None

    def fail(self, err: str) -> None:
        self.consecutive_fails += 1
        self.last_error = err[:500]

    def fresh(self) -> bool:
        """最近成功 tick 是否在容忍窗内。窗口 > 30s 错误退避上限，单次 DB 抖动不打翻 readiness。"""
        base = self.last_ok_at if self.last_ok_at is not None else self.started_at
        return (time.monotonic() - base) <= max(10 * self.interval_s, 35.0)


async def try_lock(conn) -> bool:
    """在给定连接上尝试拿单实例 advisory lock（非阻塞）。

    注意：锁随**会话**存续。SQLAlchemy 的 conn.close() 只是把连接还回池子（会话未断，锁仍持有），
    优雅关停必须先 :func:`unlock` 再还连接；进程退出时会话断开自动释放。
    """
    return bool((await conn.exec_driver_sql(f"SELECT pg_try_advisory_lock({LOCK_KEY})")).scalar())


async def unlock(conn) -> None:
    """显式释放单实例锁（优雅关停/测试用；进程退出时无需调用）。"""
    await conn.exec_driver_sql(f"SELECT pg_advisory_unlock({LOCK_KEY})")
