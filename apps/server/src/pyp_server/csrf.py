"""CSRF 防护（双提交 Cookie 模式）——SSR 状态变更表单的纵深防御。

与既有 `SameSite=Lax` 会话 cookie 叠加：Lax 已阻止跨站 POST 携带 cookie（主要防线），
本模块再加一层：渲染表单时下发随机 `pyp_csrf`（HttpOnly，Lax）并把同值注入隐藏字段，
POST 时比对表单值与 cookie（常量时间）——跨站攻击者既拿不到 token、跨站 POST 也不带 cookie，双重落空。
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, Response

CSRF_COOKIE = "pyp_csrf"
CSRF_FIELD = "csrf_token"
_MAX_AGE = 12 * 3600


def _set_cookie(request: Request, resp: Response, token: str) -> None:
    if request.cookies.get(CSRF_COOKIE) != token:
        resp.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax", max_age=_MAX_AGE)


def render_with_csrf(request: Request, template: str, context: dict, *, status_code: int = 200) -> Response:
    """渲染 SSR 模板并保证 CSRF token 就绪：注入 `csrf_token` 到上下文 + 下发/复用 `pyp_csrf` cookie。"""
    token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    resp = request.app.state.templates.TemplateResponse(
        request, template, {**context, CSRF_FIELD: token}, status_code=status_code
    )
    _set_cookie(request, resp, token)
    return resp


def verify_csrf(request: Request, submitted: str | None) -> None:
    """校验表单提交的 token 与 cookie 一致；不符/缺失 → 403。"""
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    if not submitted or not cookie or not hmac.compare_digest(submitted, cookie):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面后重试")
