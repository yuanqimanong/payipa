"""后台任务执行原语：阶段隔离、低频调度、心跳与退避；不依赖 HTTP 或应用状态。"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
from jianbing_utils.retry import backoff_delay

from pyp_server.runtime import LoopHealth


@dataclass(slots=True)
class PeriodicStage:
    """interval_s=0 表示每轮执行；失败不推进时间，下轮可重试。"""

    name: str
    action: Callable[[], Awaitable[Any]]
    interval_s: float = 0.0
    _next_run_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_s) or self.interval_s < 0:
            raise ValueError("stage interval_s 必须是有限非负数")

    async def run(self, now: float, logger: logging.Logger) -> bool:
        if now < self._next_run_at:
            return True
        try:
            await self.action()
        except Exception:  # anyio/asyncio 取消是 BaseException，继续向上传播
            logger.exception("background stage %r failed", self.name)
            return False
        self._next_run_at = now + self.interval_s
        return True


async def run_stages(stages: Sequence[PeriodicStage], logger: logging.Logger) -> bool:
    """执行全部到期阶段；即使前序失败，后续阶段也照常执行。"""
    now = time.monotonic()
    ok = True
    for stage in stages:
        succeeded = await stage.run(now, logger)
        ok = ok and succeeded
    return ok


async def run_periodic(
    tick: Callable[[], Awaitable[bool]],
    *,
    interval_s: float,
    logger: logging.Logger,
    health: LoopHealth | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """长驻执行 tick；连续失败指数退避，成功恢复正常间隔，取消立即传播。"""
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("interval_s 必须是有限正数")
    do_sleep = sleep if sleep is not None else anyio.sleep
    fails = 0
    while True:
        error = "background tick had a failing stage"
        try:
            ok = await tick()
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("background tick failed")
        if ok:
            fails = 0
            if health is not None:
                health.ok()
            delay = interval_s
        else:
            fails += 1
            if health is not None:
                health.fail(error)
            # 保持原有最多 5 次倍增、30 秒封顶的后台环策略；计算统一由工具库提供。
            delay = backoff_delay(min(fails, 6), base=interval_s, cap=30.0)
            logger.warning("background tick failed (x%d); backoff %.1fs", fails, delay)
        await do_sleep(delay)
