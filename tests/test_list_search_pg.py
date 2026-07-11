"""清单页服务端搜索（§10.7）：list_users/list_tasks 的 q 过滤 + SSR /sources?q= 过滤。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa import views
from payipa.db.pyp import Source, Task, User
from payipa.db.settings import get_settings
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_USER = "lsearch-probe-user"
_PW = "abcd1234"
_DISPLAY = "搜索甲"
_UUID = "lsearch_probe"
_NAME = "搜索测试源"


async def _seed() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                pg_insert(User.__table__)
                .values(username=_USER, password_hash=hash_password(_PW), display_name=_DISPLAY, status="active")
                .on_conflict_do_nothing(index_elements=["username"])
            )
            sid = (
                await conn.execute(pg_insert(Source.__table__).values(uuid=_UUID, name=_NAME).returning(Source.id))
            ).scalar_one()
            await conn.execute(
                pg_insert(Task.__table__).values(source_id=sid, trigger_type="manual", priority="mid", params={})
            )
    finally:
        await pyp.dispose()


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with pyp.begin() as conn:
            await conn.execute(
                text("DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)"), {"u": _UUID}
            )
            await conn.execute(text("DELETE FROM sources WHERE uuid=:u"), {"u": _UUID})
            await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _USER})
    finally:
        await pyp.dispose()


def test_views_q_filter(require_pg: None) -> None:
    """list_users / list_tasks 的 q：命中含目标行，不命中为空。"""
    asyncio.run(_cleanup())
    asyncio.run(_seed())
    try:

        async def _check() -> None:
            pyp = create_async_engine(get_settings().async_url("pyp"))
            try:
                # 用户名命中 + 显示名命中 + 不命中
                hit = await views.list_users(pyp, q="lsearch-probe")
                assert any(u["username"] == _USER for u in hit)
                hit_display = await views.list_users(pyp, q=_DISPLAY)
                assert any(u["username"] == _USER for u in hit_display)
                miss = await views.list_users(pyp, q="zzz-no-such-user")
                assert all(u["username"] != _USER for u in miss)

                # 任务按源名命中 + 不命中
                hit_tasks = await views.list_tasks(pyp, q=_NAME)
                assert hit_tasks and all(t["source_name"] == _NAME for t in hit_tasks)
                assert await views.list_tasks(pyp, q="zzz-no-such-source") == []
            finally:
                await pyp.dispose()

        asyncio.run(_check())
    finally:
        asyncio.run(_cleanup())


def test_sources_ssr_search(require_pg: None) -> None:
    """SSR /sources?q=：名称/短码模糊过滤生效，不命中时该源不出现。"""
    asyncio.run(_cleanup())
    asyncio.run(_seed())
    try:
        with TestClient(create_app()) as client:  # 上下文管理器：单事件循环服务全部请求
            client.get("/login")
            client.post(
                "/login",
                data={"username": _USER, "password": _PW, "csrf_token": client.cookies.get("pyp_csrf")},
                follow_redirects=False,
            )
            # 按名称命中
            r = client.get("/sources", params={"q": _NAME})
            assert r.status_code == 200 and _UUID in r.text
            # 按短码命中
            r = client.get("/sources", params={"q": "lsearch_pro"})
            assert r.status_code == 200 and _NAME in r.text
            # 不命中：目标源不出现
            r = client.get("/sources", params={"q": "zzz-no-such"})
            assert r.status_code == 200 and _UUID not in r.text
    finally:
        asyncio.run(_cleanup())
