"""管理界面只读数据端点（M5 前端）：把 core.views 的聚合行以 JSON 暴露给页面 fetch。

每个端点经对应 RBAC 读权限闸门（settings.rbac_enabled 关时直通，开时按权限矩阵）。
页面外壳仍由 ui.py 服务端渲染（登录保护），数据经这里的 /api/views/* 拉取——与 dashboard 同范式。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from payipa import views
from payipa.db.engine import get_engine

from pyp_server.auth import require_perm, require_user
from pyp_server.settings import get_server_settings

# 路由级强制登录：即便 RBAC 关闭（require_perm 直通），这些数据端点也必须已登录——它们回显
# 用户清单/审计日志等敏感数据，与其背后受登录保护的页面外壳（ui.py）保持一致，杜绝匿名直连读取。
router = APIRouter(prefix="/api/views", tags=["views"], dependencies=[Depends(require_user)])


@router.get("/join-info", summary="节点接入信息", dependencies=[Depends(require_perm("agents.read"))])
async def view_join_info(request: Request) -> dict:
    """节点接入引导用：主控地址推断 + join token 展示策略（dev 明示，生产不回显真值）。"""
    settings = get_server_settings()
    host = request.headers.get("host") or "127.0.0.1:8000"
    token = settings.agent_join_token
    return {
        "server_url": f"{request.url.scheme}://{host}",
        "join_token": token if token == "dev" else None,  # 生产不回显真 token（前端显占位）
    }


@router.get("/tasks", summary="任务清单", dependencies=[Depends(require_perm("tasks.read"))])
async def view_tasks(limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    return await views.list_tasks(get_engine("pyp"), limit=limit)


@router.get("/audit", summary="审计日志", dependencies=[Depends(require_perm("audit.read"))])
async def view_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None, max_length=64),
) -> list[dict]:
    return await views.list_audit(get_engine("pyp"), limit=limit, offset=offset, action=action)


@router.get("/assemblies", summary="组装产物清单", dependencies=[Depends(require_perm("assemblies.read"))])
async def view_assemblies(limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    return await views.list_assemblies(get_engine("pyp"), limit=limit)


@router.get("/push", summary="推送组件 + outbox 汇总", dependencies=[Depends(require_perm("push.read"))])
async def view_push(limit: int = Query(100, ge=1, le=500)) -> dict:
    engine = get_engine("pyp")
    return {
        "components": await views.list_push_components(engine, limit=limit),
        "outbox": await views.outbox_summary(engine),
    }


@router.get("/rules", summary="规则清单", dependencies=[Depends(require_perm("rules.read"))])
async def view_rules(limit: int = Query(200, ge=1, le=500)) -> list[dict]:
    return await views.list_rules(get_engine("pyp"), limit=limit)


@router.get("/users", summary="用户清单", dependencies=[Depends(require_perm("users.manage"))])
async def view_users(limit: int = Query(200, ge=1, le=500)) -> list[dict]:
    return await views.list_users(get_engine("pyp"), limit=limit)


@router.get("/roles", summary="角色清单", dependencies=[Depends(require_perm("roles.manage"))])
async def view_roles() -> list[dict]:
    return await views.list_roles(get_engine("pyp"))


@router.get("/config", summary="公共配置总览", dependencies=[Depends(require_perm("config.manage"))])
async def view_config() -> dict:
    return await views.config_overview(get_engine("pyp"))


@router.get("/storage", summary="存储总览", dependencies=[Depends(require_perm("storage.manage"))])
async def view_storage() -> dict:
    return await views.storage_overview(get_engine("pyp"))
