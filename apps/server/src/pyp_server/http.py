"""HTTP 中间件装配：Host 白名单与响应头策略。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI

    from pyp_server.settings import ServerSettings


def install_http_middleware(app: FastAPI, settings: ServerSettings) -> None:
    allowed_hosts = [value.strip() for value in settings.allowed_hosts.split(",") if value.strip()]
    if allowed_hosts and allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if settings.environment == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
