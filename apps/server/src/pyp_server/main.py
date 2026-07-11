"""FastAPI 应用工厂：装配 payipa-core（同进程直接函数调用，非网络 API）。

M0 空壳：健康检查 + 契约 stub API + agent WS 握手 + OpenAPI（/openapi.json、/docs）。
启动：``uv run uvicorn pyp_server.main:app``（不依赖活 DB，引擎懒建）。
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import anyio.abc
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from payipa.db.engine import get_engine

from pyp_server.consumer import consumer_loop
from pyp_server.hub import AgentHub
from pyp_server.preflight import run_preflight
from pyp_server.ratelimit import SourceRateLimiter
from pyp_server.routers import (
    ai,
    api,
    auth_routes,
    config_mgmt,
    datasets,
    explore,
    health,
    internal,
    manage,
    onboard,
    ops,
    setup,
    sources,
    studio,
    ui,
    views,
    ws,
)
from pyp_server.runtime import LoopHealth, try_lock, unlock
from pyp_server.scheduler import dispatch_loop
from pyp_server.settings import get_server_settings

_HERE = Path(__file__).parent
logger = logging.getLogger("pyp_server.main")


async def _guarded_loops(app: FastAPI, tg: anyio.abc.TaskGroup) -> None:
    """先拿单实例锁（P0-09）再启动后台环：同库第二个进程/worker 拒绝启动。

    PG 未起：退避重试（保持「无 DB 也能启动」，readyz 期间报后台环未就绪）；
    锁被他人持有：短暂等待（容忍滚动重启交接）后**主动触发优雅关停**——
    task group 里抛异常会被困到 lifespan 关停才浮出（进程照常服务），发 SIGINT 才是真拒绝。
    """
    settings = get_server_settings()
    engine = get_engine("pyp")
    held_since: float | None = None
    delay = 1.0
    while True:
        conn = None
        try:
            conn = await engine.connect()
            if await try_lock(conn):
                app.state.lock_conn = conn  # 持有到进程退出；连接关闭即自动释放
                break
            await conn.aclose()
            if held_since is None:
                held_since = time.monotonic()
            elif time.monotonic() - held_since > 15:
                logger.critical(
                    "另一进程已持有 payipa 单实例锁——v1 只支持单主控实例、单 uvicorn worker（workers=1）；本进程退出"
                )
                signal.raise_signal(signal.SIGINT)  # 交给 uvicorn 优雅关停（P0-09 启动即拒绝）
                return
        except Exception:  # noqa: BLE001 —— PG 未起/抖动：退避重试
            if conn is not None:
                await conn.aclose()
            logger.warning("acquire singleton lock failed (DB down?); retry in %.0fs", delay, exc_info=True)
        await anyio.sleep(delay)
        delay = min(delay * 2, 30.0)
    if settings.dispatch_enabled:
        tg.start_soon(dispatch_loop, app)
    if settings.push_enabled:
        tg.start_soon(consumer_loop, app)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """服务生命周期：在 anyio 结构化并发下拉起后台派发环 + 推送 Consumer，关停时整树取消。"""
    settings = get_server_settings()
    app.state.loop_health = {}  # readyz 据此判断后台环心跳（P0-06）
    app.state.lock_conn = None
    if not (settings.dispatch_enabled or settings.push_enabled):
        yield  # 测试/特殊部署：不启后台环
        return
    if settings.dispatch_enabled:
        app.state.loop_health["dispatch"] = LoopHealth("dispatch", settings.dispatch_interval_s)
    if settings.push_enabled:
        app.state.loop_health["consumer"] = LoopHealth("consumer", settings.push_interval_s)
    async with anyio.create_task_group() as tg:
        if settings.single_worker_guard:
            tg.start_soon(_guarded_loops, app, tg)
        else:
            if settings.dispatch_enabled:
                tg.start_soon(dispatch_loop, app)
            if settings.push_enabled:
                tg.start_soon(consumer_loop, app)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()
            if app.state.lock_conn is not None:
                try:
                    await unlock(app.state.lock_conn)  # close 只还池不断会话，须显式释放锁
                finally:
                    await app.state.lock_conn.aclose()


def create_app() -> FastAPI:
    settings = get_server_settings()
    run_preflight(settings)  # production 模式拒绝不安全配置；dev 模式仅在 API 开放时告警
    app = FastAPI(
        title=settings.title,
        version=settings.version,
        description=settings.description,
        debug=settings.debug,
        lifespan=_lifespan,
    )
    app.state.hub = AgentHub()  # 在线 agent 连接注册表（进程内单例）
    app.state.limiter = SourceRateLimiter()  # 每源令牌桶 + AIMD（派发环限流、结果回报调频）
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(setup.router)
    app.include_router(onboard.router)
    app.include_router(ops.router)
    app.include_router(ui.router)
    app.include_router(sources.router)
    app.include_router(api.router)
    app.include_router(views.router)
    app.include_router(manage.router)
    app.include_router(studio.router)
    app.include_router(config_mgmt.router)
    app.include_router(explore.router)
    app.include_router(ai.router)
    app.include_router(internal.router)
    app.include_router(datasets.router)
    app.include_router(ws.router)

    # SSR（06 定案）：模板与静态资源目录
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))
    static_dir = _HERE / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
