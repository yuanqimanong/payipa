"""鉴权：argon2 密码哈希 + PyJWT 签名会话（HttpOnly cookie）+ current_user / require_perm 依赖（06 定案）。

会话走 HttpOnly cookie（防 XSS）；程序化/对外用 PyJWT/api_key。RBAC（M5）：require_perm 闸门，
由 payipa.security.rbac 解析有效权限（角色∪直授，管理员通配 ``*``）；开关 settings.rbac_enabled。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from fastapi import HTTPException, Request
from payipa.db.engine import get_engine
from payipa.db.pyp import User
from payipa.security.rbac import effective_permissions, has_permission
from sqlalchemy import select

from pyp_server.settings import get_server_settings

COOKIE_NAME = "pyp_session"
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except Argon2Error, ValueError, TypeError:
        return False


def create_session(user_id: int, username: str) -> str:
    s = get_server_settings()
    payload = {"sub": str(user_id), "u": username, "exp": int(time.time()) + s.session_ttl_s}
    return jwt.encode(payload, s.session_secret, algorithm="HS256")


def _decode(token: str) -> dict | None:
    try:
        return jwt.decode(token, get_server_settings().session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


async def get_current_user(request: Request) -> dict | None:
    """从 cookie 解析会话并核验用户仍 active；返回 {id, username} 或 None。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    claims = _decode(token)
    if not claims:
        return None
    async with get_engine("pyp").connect() as conn:
        row = (
            await conn.execute(select(User.id, User.username, User.status).where(User.id == int(claims["sub"])))
        ).first()
    if row is None or row[2] != "active":
        return None
    return {"id": row[0], "username": row[1]}


async def require_user(request: Request) -> dict:
    """JSON API 依赖：未登录抛 401。"""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    return user


def require_perm(code: str) -> Callable[[Request], Awaitable[dict | None]]:
    """RBAC 权限闸门依赖工厂。rbac_enabled=False → 直通（保持现网开放）；
    True → 未登录 401、缺权限 403（管理员通配 ``*`` 放行）。返回当前用户（关时 None）。"""

    async def _dep(request: Request) -> dict | None:
        if not get_server_settings().rbac_enabled:
            return None  # 闸门未启用：直通
        user = await require_user(request)
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, code):
            raise HTTPException(status_code=403, detail=f"missing permission: {code}")
        return user

    return _dep
