"""建源表单错误回填（P0-25）：校验失败时回显已提交值、不建行。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import Source, User
from payipa.db.settings import get_settings
from payipa.security.rbac import make_superuser, seed_default_rbac
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_ADMIN = "srcform-probe-admin"
_PW = "abcd1234"
_UUID = "srcform_probe"


async def _ensure_admin() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                pg_insert(User.__table__)
                .values(username=_ADMIN, password_hash=hash_password(_PW), status="active")
                .on_conflict_do_nothing(index_elements=["username"])
            )
        await seed_default_rbac(pyp)
        await make_superuser(pyp, _ADMIN)
    finally:
        await pyp.dispose()


def test_create_error_refills_input(require_pg: None) -> None:
    asyncio.run(_ensure_admin())
    try:
        with TestClient(create_app()) as client:  # 上下文管理器：单事件循环服务全部请求
            client.get("/login")
            client.post(
                "/login",
                data={"username": _ADMIN, "password": _PW, "csrf_token": client.cookies.get("pyp_csrf")},
                follow_redirects=False,
            )
            client.get("/sources/new")
            token = client.cookies.get("pyp_csrf")
            # 非法：rate_limit 超范围（1–1000）→ dispatch_source_run 抛 ValueError，页面回显
            r = client.post(
                "/sources/create",
                data={
                    "name": "回填测试源",
                    "uuid": _UUID,
                    "seed_urls": "https://books.toscrape.com/",
                    "access_basis": "public_policy",
                    "access_reference": "公开练习站",
                    "access_confirmed": "on",
                    "engine_hint": "http",
                    "rate_limit": "99999",  # 越界
                    "retry": "3",
                    "timeout": "30",
                    "item_locator": "article.product_pod",
                    "field_name": "title",
                    "field_css": "h3 a@title",
                    "field_type": "store",
                    "fingerprint": "title",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert r.status_code == 400
            # 回填：刚提交的名称/短码/URL/字段仍在页面里
            assert "回填测试源" in r.text
            assert _UUID in r.text
            assert "books.toscrape.com" in r.text
            assert "h3 a@title" in r.text

            # 表单停留过久导致 CSRF cookie 过期时，也应回填全部输入并签发新 token。
            client.cookies.delete("pyp_csrf")
            expired = client.post(
                "/sources/create",
                data={
                    "name": "令牌过期仍回填",
                    "uuid": _UUID,
                    "seed_urls": "https://books.toscrape.com/",
                    "access_basis": "public_policy",
                    "access_reference": "公开练习站",
                    "access_confirmed": "on",
                    "field_name": "title",
                    "field_css": "h3 a@title",
                    "field_type": "store",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert expired.status_code == 403
            assert "令牌过期仍回填" in expired.text
            assert "h3 a@title" in expired.text
            assert client.cookies.get("pyp_csrf")

        # 未建行
        async def _exists() -> bool:
            pyp = create_async_engine(get_settings().async_url("pyp"))
            try:
                async with pyp.connect() as conn:
                    return (await conn.execute(select(Source.id).where(Source.uuid == _UUID))).first() is not None
            finally:
                await pyp.dispose()

        assert asyncio.run(_exists()) is False
    finally:

        async def _cleanup() -> None:
            pyp = create_async_engine(get_settings().async_url("pyp"))
            try:
                async with pyp.begin() as conn:
                    await conn.execute(text("DELETE FROM sources WHERE uuid=:u"), {"u": _UUID})
                    await conn.execute(
                        text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username=:n)"),
                        {"n": _ADMIN},
                    )
                    await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _ADMIN})
            finally:
                await pyp.dispose()

        asyncio.run(_cleanup())
