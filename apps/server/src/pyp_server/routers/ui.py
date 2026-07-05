"""前端页面路由：仪表盘 + 尚未实现页（mock 数据占位）。页面级登录保护（未登录跳 /login）。

未实现页统一走 mock_page.html + static/mock.json；功能实现后删除对应路由与 mock 条目即可。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pyp_server.auth import get_current_user

router = APIRouter(tags=["ui"])

# (路径, key=active, 标题)——尚未实现、以 mock 展示的页面
_MOCK_PAGES = [
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


def _make_mock_handler(key: str, title: str) -> Callable[[Request], Awaitable]:
    async def handler(request: Request):
        user = await get_current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return request.app.state.templates.TemplateResponse(
            request, "mock_page.html", {"user": user, "active": key, "page_key": key, "title": title}
        )

    return handler


for _path, _key, _title in _MOCK_PAGES:
    router.add_api_route(
        _path, _make_mock_handler(_key, _title), methods=["GET"], include_in_schema=False, response_class=HTMLResponse
    )
