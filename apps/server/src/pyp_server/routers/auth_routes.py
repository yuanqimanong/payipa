"""登录/登出（账号密码 + HttpOnly cookie 会话，06 定案；无自助注册）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.db.engine import get_engine
from payipa.db.pyp import User
from sqlalchemy import select

from pyp_server.auth import COOKIE_NAME, create_session, verify_password
from pyp_server.settings import get_server_settings

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse, summary="登录页")
async def login_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", summary="登录提交")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    async with get_engine("pyp").connect() as conn:
        row = (
            await conn.execute(
                select(User.id, User.username, User.password_hash, User.status).where(User.username == username)
            )
        ).first()
    if row is None or row[3] != "active" or not verify_password(row[2], password):
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": "用户名或密码错误"}, status_code=401
        )
    resp = RedirectResponse("/sources", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        create_session(row[0], row[1]),
        httponly=True,
        samesite="lax",
        max_age=get_server_settings().session_ttl_s,
    )  # 生产加 secure=True（HTTPS）
    return resp


@router.post("/logout", summary="登出")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp
