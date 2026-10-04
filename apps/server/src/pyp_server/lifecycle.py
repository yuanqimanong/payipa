"""主控生命周期：单实例锁、后台任务树与连接池释放。"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from payipa.db.engine import get_engine, get_sessionmaker

from pyp_server.consumer import consumer_loop
from pyp_server.runtime import LoopHealth, try_lock, unlock
from pyp_server.scheduler import dispatch_loop

if TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from fastapi import FastAPI

logger = logging.getLogger("pyp_server.lifecycle")


def _start_loops(app: FastAPI, group: TaskGroup) -> None:
    settings = app.state.settings
    if settings.dispatch_enabled:
        group.start_soon(dispatch_loop, app)
    if settings.push_enabled:
        group.start_soon(consumer_loop, app)


async def _guarded_loops(app: FastAPI, group: TaskGroup) -> None:
    """拿到 PG 会话锁后启动 worker；DB 不可达时退避，多实例冲突时优雅关停。"""
    engine = get_engine("pyp")
    held_since: float | None = None
    delay = 1.0
    while True:
        conn = None
        try:
            conn = await engine.connect()
            if await try_lock(conn):
                app.state.lock_conn = conn
                break
            if held_since is None:
                held_since = time.monotonic()
            elif time.monotonic() - held_since > 15:
                logger.critical("另一进程持有 payipa 单实例锁；v1 只支持单主控、单 worker，本进程退出")
                signal.raise_signal(signal.SIGINT)
                return
        except Exception:
            logger.warning("acquire singleton lock failed; retry in %.0fs", delay, exc_info=True)
        except BaseException:
            # 取消可能发生在 PG 已获锁、结果尚未返回的窗口；物理断会话才保证未知锁被释放。
            if conn is not None:
                with anyio.CancelScope(shield=True):
                    await conn.invalidate()
            raise
        finally:
            if conn is not None and conn is not app.state.lock_conn:
                with anyio.CancelScope(shield=True):
                    await conn.aclose()
        await anyio.sleep(delay)
        delay = min(delay * 2, 30.0)
    _start_loops(app, group)


async def _release_resources(app: FastAPI) -> None:
    errors: list[Exception] = []
    conn = app.state.lock_conn
    if conn is not None:
        try:
            await unlock(conn)
        except Exception as exc:
            errors.append(exc)
            # close 仅还连接池，unlock 失败必须断开会话以释放锁。
            try:
                await conn.invalidate()
            except Exception as invalidation_error:
                errors.append(invalidation_error)
        finally:
            try:
                await conn.aclose()
            except Exception as close_error:
                errors.append(close_error)
            app.state.lock_conn = None
    for key in ("pyp", "data_center", "business"):
        try:
            await get_engine(key).dispose()
        except Exception as exc:
            errors.append(exc)
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
    if errors:
        raise ExceptionGroup("主控资源释放失败", errors)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """先取消并等待后台任务退出，再释放单实例锁；清理过程屏蔽外层取消。"""
    app.state.lock_conn = None
    app.state.loop_health = {}
    app.state.readiness_cache = {"at": 0.0, "resp": None}
    try:
        settings = app.state.settings
        if not (settings.dispatch_enabled or settings.push_enabled):
            yield
            return
        if settings.dispatch_enabled:
            app.state.loop_health["dispatch"] = LoopHealth("dispatch", settings.dispatch_interval_s)
        if settings.push_enabled:
            app.state.loop_health["consumer"] = LoopHealth("consumer", settings.push_interval_s)
        async with anyio.create_task_group() as group:
            if settings.single_worker_guard:
                group.start_soon(_guarded_loops, app, group)
            else:
                _start_loops(app, group)
            try:
                yield
            finally:
                group.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await _release_resources(app)
