"""首次启动引导（P0-05 页面化）：系统未初始化（users 表为空）时引导创建首个管理员。

有任意用户后本页永久关闭（跳登录），不构成公开建号面。密码只走表单 POST，
不进 shell history / 进程列表（对 CLI 建号方式的补充，二者并存）。
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.db.engine import get_engine
from payipa.db.pyp import User
from payipa.security.rbac import make_superuser, seed_default_rbac
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from pyp_server.auth import hash_password
from pyp_server.csrf import render_with_csrf, verify_csrf
from pyp_server.routers.health import readyz
from pyp_server.settings import get_server_settings

router = APIRouter(tags=["setup"])
logger = logging.getLogger("pyp_server.setup")


async def no_users(engine: AsyncEngine) -> bool:
    """系统是否未初始化（users 表为空）。DB 不可达时按「已初始化」处理（不开建号面）。"""
    try:
        async with engine.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(User.__table__))).scalar() == 0
    except Exception:  # noqa: BLE001
        return False


async def _checks(request: Request) -> dict[str, str]:
    """复用 /readyz 的分项检查（同一份真相，不另写一套）。"""
    resp = await readyz(request)
    import json

    return json.loads(bytes(resp.body))["checks"]


_TIPS = {  # 分项不绿时给安装者的修复提示
    "db": "确认 PostgreSQL 已启动、.env 的 PG_* 配置正确、三个数据库已创建",
    "migrations": "在仓库根执行：uv run alembic -c deploy/alembic.ini upgrade heads",
    "storage": "检查数据目录磁盘剩余空间（低于水位会拒绝写入）",
}


def _tip(key: str) -> str:
    return _TIPS["db"] if key.startswith("db.") else _TIPS.get(key, "")


@router.get("/setup", response_class=HTMLResponse, summary="首次启动引导", include_in_schema=False)
async def setup_page(request: Request):
    if not await no_users(get_engine("pyp")):
        return RedirectResponse("/login", status_code=303)
    return render_with_csrf(
        request, "setup.html", {"checks": await _checks(request), "tip": _tip, "error": None, "username": ""}
    )


@router.post("/setup", summary="创建首个管理员", include_in_schema=False)
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    bootstrap_token: str = Form(...),
    csrf_token: str = Form(None),
):
    verify_csrf(request, csrf_token)
    engine = get_engine("pyp")

    async def back(error: str, status: int = 400):
        return render_with_csrf(
            request,
            "setup.html",
            {"checks": await _checks(request), "tip": _tip, "error": error, "username": username},
            status_code=status,
        )

    username = username.strip()
    if not hmac.compare_digest(bootstrap_token, get_server_settings().bootstrap_token):
        logger.warning("rejected first-admin setup attempt with invalid bootstrap token")
        return await back("安装码无效，请使用部署时生成的 PYP_SERVER_BOOTSTRAP_TOKEN", status=403)
    if not 3 <= len(username) <= 64:
        return await back("用户名长度须在 3–64 个字符之间")
    if len(password) < 8:
        return await back("密码至少 8 个字符")
    if password != password2:
        return await back("两次输入的密码不一致")

    async with engine.begin() as conn:
        # 事务内二次确认仍未初始化：并发提交只有一个能建号
        count = (await conn.execute(select(func.count()).select_from(User.__table__))).scalar()
        if count != 0:
            return RedirectResponse("/login", status_code=303)
        await conn.execute(
            pg_insert(User.__table__).values(username=username, password_hash=hash_password(password), status="active")
        )
    await seed_default_rbac(engine)  # 播种权限目录 + 默认角色（幂等）
    await make_superuser(engine, username)  # 首个账号即管理员
    logger.info("first admin %r created via /setup", username)
    return RedirectResponse("/login", status_code=303)
