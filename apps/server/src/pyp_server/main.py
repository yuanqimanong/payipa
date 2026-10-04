"""FastAPI 应用工厂：装配 payipa-core（同进程直接函数调用，非网络 API）。

入口仅负责 HTTP、中间件、运行状态与路由装配；生命周期和业务编排各由专属模块负责。
启动：``uv run uvicorn pyp_server.main:app``（不依赖活 DB，引擎懒建）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pyp_server.http import install_http_middleware
from pyp_server.hub import AgentHub
from pyp_server.lifecycle import lifespan
from pyp_server.login_guard import LoginThrottle
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
from pyp_server.settings import get_server_settings

_HERE = Path(__file__).parent


def create_app() -> FastAPI:
    settings = get_server_settings()
    run_preflight(settings)  # production 模式拒绝不安全配置；dev 模式仅在 API 开放时告警
    app = FastAPI(
        title=settings.title,
        version=settings.version,
        description=settings.description,
        debug=settings.debug,
        lifespan=lifespan,
    )
    install_http_middleware(app, settings)
    app.state.settings = settings
    app.state.loop_health = {}
    app.state.readiness_cache = {"at": 0.0, "resp": None}
    app.state.hub = AgentHub()  # 在线 agent 连接注册表（进程内单例）
    app.state.limiter = SourceRateLimiter()  # 每源令牌桶 + AIMD（派发环限流、结果回报调频）
    app.state.login_throttle = LoginThrottle()  # 登录失败节流：抵御在线暴力破解（进程内、按 IP+用户名）
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
