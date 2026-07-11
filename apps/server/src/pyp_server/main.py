"""FastAPI 应用工厂：装配 payipa-core（同进程直接函数调用，非网络 API）。

M0 空壳：健康检查 + 契约 stub API + agent WS 握手 + OpenAPI（/openapi.json、/docs）。
启动：``uv run uvicorn pyp_server.main:app``（不依赖活 DB，引擎懒建）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
    sources,
    studio,
    ui,
    views,
    ws,
)
from pyp_server.scheduler import dispatch_loop
from pyp_server.settings import get_server_settings

_HERE = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """服务生命周期：在 anyio 结构化并发下拉起后台派发环 + 推送 Consumer，关停时整树取消。"""
    settings = get_server_settings()
    if not (settings.dispatch_enabled or settings.push_enabled):
        yield  # 测试/特殊部署：不启后台环
        return
    async with anyio.create_task_group() as tg:
        if settings.dispatch_enabled:
            tg.start_soon(dispatch_loop, app)
        if settings.push_enabled:
            tg.start_soon(consumer_loop, app)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


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
