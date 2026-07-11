"""登录 + 建源界面集成测试（需 PG）：账号登录、页面保护、建源表单 → 建表。

注：DB 准备/清理/核对用独立 create_async_engine（各自 asyncio.run 事件循环），HTTP 用 `with TestClient`
（单一 portal 事件循环）—— 二者不共享缓存的 async 引擎，避免跨 loop。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import Source, User
from payipa.db.settings import get_settings
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_USER = "tester"
_PW = "s3cret-pw"
_UUID = "authuitest"


async def _seed_user() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username == _USER))
        await conn.execute(
            pg_insert(User.__table__).values(username=_USER, password_hash=hash_password(_PW), status="active")
        )
    await engine.dispose()


async def _cleanup() -> None:
    dc = create_async_engine(get_settings().async_url("data_center"))
    async with dc.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS data_{_UUID}"))
    await dc.dispose()
    pyp = create_async_engine(get_settings().async_url("pyp"))
    async with pyp.begin() as conn:
        for sql in (
            "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
            "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
            "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
            "DELETE FROM sources WHERE uuid=:u",
        ):
            await conn.execute(text(sql), {"u": _UUID})
        await conn.execute(delete(User.__table__).where(User.username == _USER))
    await pyp.dispose()


async def _source_and_table_exist() -> tuple[bool, bool]:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    async with pyp.connect() as conn:
        src = (await conn.execute(select(Source.uuid).where(Source.uuid == _UUID))).first()
    await pyp.dispose()
    dc = create_async_engine(get_settings().async_url("data_center"))
    async with dc.connect() as conn:
        reg = (await conn.execute(text(f"SELECT to_regclass('public.data_{_UUID}')"))).scalar()
    await dc.dispose()
    return src is not None, reg is not None


def test_login_and_create_source(require_pg: None) -> None:
    asyncio.run(_cleanup())
    asyncio.run(_seed_user())
    try:
        with TestClient(create_app()) as client:
            # 未登录 → 跳登录
            r = client.get("/sources", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/login"

            # GET 登录页下发 CSRF token（双提交 cookie）；后续 POST 复用
            client.get("/login")
            tok = client.cookies.get("pyp_csrf")
            assert tok

            # 缺 CSRF token → 403
            no_csrf = client.post("/login", data={"username": _USER, "password": _PW}, follow_redirects=False)
            assert no_csrf.status_code == 403

            # 错误密码 → 401
            bad = client.post(
                "/login", data={"username": _USER, "password": "wrong", "csrf_token": tok}, follow_redirects=False
            )
            assert bad.status_code == 401

            # 正确登录 → 303 + 会话 cookie
            ok = client.post(
                "/login", data={"username": _USER, "password": _PW, "csrf_token": tok}, follow_redirects=False
            )
            assert ok.status_code == 303
            assert client.cookies.get("pyp_session")

            # 登录后可访问列表页与查看页（含 vendored Tabulator）
            assert client.get("/sources").status_code == 200
            page = client.get(f"/data/{_UUID}")
            assert page.status_code == 200
            assert "tabulator.min.js" in page.text

            # 建源表单 → 303 跳查看页
            form = {
                "name": "Auth UI Test",
                "uuid": _UUID,
                "seed_urls": "https://books.toscrape.com/",
                "access_basis": "public_policy",
                "access_reference": "https://books.toscrape.com/",
                "access_confirmed": "on",
                "item_locator": "article.product_pod",
                "field_name": "title",
                "field_css": "h3 a@title",
                "field_type": "store",
                "fingerprint": "title",
                "csrf_token": tok,
            }
            created = client.post("/sources/create", data=form, follow_redirects=False)
            assert created.status_code == 303
            assert created.headers["location"] == f"/data/{_UUID}"
            review_page = client.get(f"/sources/{_UUID}/access-review")
            assert review_page.status_code == 200
            # 复核「维持暂停」→ 源保持暂停，页面回显暂停原因
            rejected = client.post(
                f"/sources/{_UUID}/access-review",
                data={
                    "access_basis": "public_policy",
                    "access_reference": "https://books.toscrape.com/",
                    "reason": "pending manual re-check",
                    "decision": "pause",
                    "csrf_token": tok,
                },
                follow_redirects=False,
            )
            assert rejected.status_code == 303
            paused_page = client.get(f"/sources/{_UUID}/access-review")
            assert paused_page.status_code == 200
            assert "pending manual re-check" in paused_page.text
            reviewed = client.post(
                f"/sources/{_UUID}/access-review",
                data={
                    "access_basis": "public_policy",
                    "access_reference": "https://books.toscrape.com/",
                    "reason": "reviewed in UI test",
                    "decision": "approve",
                    "csrf_token": tok,
                },
                follow_redirects=False,
            )
            assert reviewed.status_code == 303
            assert reviewed.headers["location"] == "/sources"

        src_ok, table_ok = asyncio.run(_source_and_table_exist())
        assert src_ok  # 源已建
        assert table_ok  # data_ 表已建
    finally:
        asyncio.run(_cleanup())
