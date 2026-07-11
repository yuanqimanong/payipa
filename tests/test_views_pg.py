"""管理界面只读视图端点 + 页面路由集成测试（需 PG）。

覆盖：登录后 12 个功能页 SSR 200 + 引用 views.js；/api/views/* 返回 JSON（形状正确）；
RBAC 开启时无权限用户被 403（数据端点强制，页面外壳仍可进）。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_USER = "viewstester"
_PW = "views-pw-123"

_PAGES = [
    "tasks", "rules", "assemblies", "push", "users", "roles",
    "config", "audit", "nodes", "monitor", "storage", "logs",
]  # fmt: skip
_JSON_VIEWS = ["tasks", "rules", "assemblies", "users", "roles", "audit", "config", "storage", "push"]


async def _seed_user() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username == _USER))
        await conn.execute(
            pg_insert(User.__table__).values(username=_USER, password_hash=hash_password(_PW), status="active")
        )
    await engine.dispose()


async def _cleanup() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username == _USER))
    await engine.dispose()


def test_view_pages_and_endpoints(require_pg: None) -> None:
    asyncio.run(_cleanup())
    asyncio.run(_seed_user())
    try:
        with TestClient(create_app()) as client:
            # 未登录 → 页面跳登录；数据端点即便 RBAC 关闭也必须 401（不得匿名读用户/审计）
            assert client.get("/monitor", follow_redirects=False).status_code == 303
            assert client.get("/api/views/users").status_code == 401
            assert client.get("/api/views/audit").status_code == 401

            client.get("/login")  # 下发 CSRF token
            tok = client.cookies.get("pyp_csrf")
            client.post("/login", data={"username": _USER, "password": _PW, "csrf_token": tok})

            # 每个功能页 SSR 200；数据驱动页引用 views.js
            for key in _PAGES:
                resp = client.get(f"/{key}")
                assert resp.status_code == 200, (key, resp.status_code)
                if key != "logs":  # logs 是纯说明页，无 fetch
                    assert "/static/views.js" in resp.text, key

            # 数据端点返回 JSON（RBAC 默认关 → 直通）
            for key in _JSON_VIEWS:
                resp = client.get(f"/api/views/{key}")
                assert resp.status_code == 200, (key, resp.status_code)
                body = resp.json()
                if key in ("config", "storage", "push"):
                    assert isinstance(body, dict), key
                else:
                    assert isinstance(body, list), key

            # config/storage/push 复合形状
            cfg = client.get("/api/views/config").json()
            assert {"models", "notify_bots", "storage"} <= cfg.keys()
            stg = client.get("/api/views/storage").json()
            assert "live" in stg and "backend" in stg["live"]
            push = client.get("/api/views/push").json()
            assert "components" in push and "outbox" in push

            # 用户视图不泄露密码 hash
            users = client.get("/api/views/users").json()
            assert all("password_hash" not in u for u in users)
    finally:
        asyncio.run(_cleanup())


def test_view_endpoints_rbac_enforced(require_pg: None, monkeypatch) -> None:
    """RBAC 开启：登录但无权限的用户访问数据端点应 403（页面外壳不受影响）。"""
    from pyp_server.settings import get_server_settings

    asyncio.run(_cleanup())
    asyncio.run(_seed_user())
    monkeypatch.setenv("PYP_SERVER_RBAC_ENABLED", "true")
    get_server_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            client.get("/login")
            tok = client.cookies.get("pyp_csrf")
            client.post("/login", data={"username": _USER, "password": _PW, "csrf_token": tok})
            # 无角色用户：页面外壳可进，数据端点 403
            assert client.get("/audit").status_code == 200
            assert client.get("/api/views/audit").status_code == 403
            assert client.get("/api/views/users").status_code == 403
    finally:
        get_server_settings.cache_clear()
        asyncio.run(_cleanup())
