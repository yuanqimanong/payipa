"""前端页面路由：仪表盘 + 各功能页（服务端渲染外壳，数据经 /api/views/* 与 /api/monitor/* 拉取）。

页面级登录保护（未登录跳 /login）。RBAC 权限在数据端点上强制——无权限的用户能进页面外壳，
但数据区显示「无权限查看」（见 static/views.js 的 403 处理），不额外拦页面。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pyp_server.auth import get_current_user

router = APIRouter(tags=["ui"])

# (路径, active/模板名, 标题)——数据驱动的真实功能页，模板 = "<key>.html"
_PAGES = [
    ("/tasks", "tasks", "任务管理"),
    ("/rules", "rules", "爬虫规则"),
    ("/assemblies", "assemblies", "数据组装"),
    ("/push", "push", "数据推送"),
    ("/users", "users", "用户管理"),
    ("/roles", "roles", "角色权限"),
    ("/config", "config", "公共配置"),
    ("/audit", "audit", "操作日志"),
    ("/nodes", "nodes", "节点管理"),
    ("/monitor", "monitor", "系统监控"),
    ("/storage", "storage", "存储管理"),
    ("/logs", "logs", "日志查看"),
]


@router.get("/", response_class=HTMLResponse, summary="仪表盘", include_in_schema=False)
async def dashboard(request: Request):
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "active": "dashboard"}
    )


def _make_page_handler(key: str, title: str):
    async def handler(request: Request):
        user = await get_current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return request.app.state.templates.TemplateResponse(
            request, f"{key}.html", {"user": user, "active": key, "title": title}
        )

    return handler


for _path, _key, _title in _PAGES:
    router.add_api_route(
        _path,
        _make_page_handler(_key, _title),
        methods=["GET"],
        include_in_schema=False,
        response_class=HTMLResponse,
    )
