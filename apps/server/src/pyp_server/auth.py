"""鉴权：argon2 密码哈希 + PyJWT 签名会话（HttpOnly cookie）+ current_user 依赖（06 定案）。

会话走 HttpOnly cookie（防 XSS）；程序化/对外用 PyJWT/api_key（后续）。RBAC 权限矩阵留后续里程碑。
"""

from __future__ import annotations

import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from fastapi import HTTPException, Request
from payipa.db.engine import get_engine
from payipa.db.pyp import User
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
