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

from pyp_server.hub import AgentHub
from pyp_server.routers import api, auth_routes, explore, health, internal, sources, ui, ws
from pyp_server.scheduler import dispatch_loop
from pyp_server.settings import get_server_settings

_HERE = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """服务生命周期：在 anyio 结构化并发下拉起后台派发环，关停时整树取消。"""
    if not get_server_settings().dispatch_enabled:
        yield  # 测试/特殊部署：不启派发环
        return
    async with anyio.create_task_group() as tg:
        tg.start_soon(dispatch_loop, app)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


def create_app() -> FastAPI:
    settings = get_server_settings()
    app = FastAPI(
        title=settings.title,
        version=settings.version,
        description=settings.description,
        debug=settings.debug,
        lifespan=_lifespan,
    )
    app.state.hub = AgentHub()  # 在线 agent 连接注册表（进程内单例）
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(ui.router)
    app.include_router(sources.router)
    app.include_router(api.router)
    app.include_router(explore.router)
    app.include_router(internal.router)
    app.include_router(ws.router)

    # SSR（06 定案）：模板与静态资源目录
    app.state.templates = Jinja2Templates(directory=str(_HERE / "templates"))
    static_dir = _HERE / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
