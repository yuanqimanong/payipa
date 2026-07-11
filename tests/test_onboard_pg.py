"""首次使用向导（P0-21）：状态读取 + 示例采集创建 + 标记完成。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.security.rbac import make_superuser, seed_default_rbac
from pyp_server.auth import hash_password
from pyp_server.main import create_app
from pyp_server.routers.onboard import DEMO_CODE
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_ADMIN = "onboard-probe-admin"
_PW = "abcd1234"


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


def _login(client: TestClient) -> None:
    client.get("/login")
    client.post(
        "/login",
        data={"username": _ADMIN, "password": _PW, "csrf_token": client.cookies.get("pyp_csrf")},
        follow_redirects=False,
    )


def test_onboard_flow(require_pg: None) -> None:
    asyncio.run(_ensure_admin())
    try:
        with TestClient(create_app()) as client:  # 上下文管理器：单事件循环服务全部请求
            _login(client)
            # 初始状态：未完成
            assert client.get("/api/onboard/state").json()["done"] is False

            # 创建示例数据源并试跑
            r = client.post("/api/onboard/demo")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["source"] == DEMO_CODE and body["batch_id"] and body["requests"] >= 1
            assert client.get("/api/onboard/state").json()["demo_created"] is True

            # 标记完成 → 状态置位
            assert client.post("/api/onboard/done").json()["done"] is True
            assert client.get("/api/onboard/state").json()["done"] is True
    finally:
        asyncio.run(_cleanup())


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    dc = create_async_engine(get_settings().async_url("data_center"))
    try:
        async with pyp.begin() as conn:
            for sql in (
                "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                "DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                "DELETE FROM sources WHERE uuid=:u",
            ):
                await conn.execute(text(sql), {"u": DEMO_CODE})
            await conn.execute(text("DELETE FROM global_params WHERE key='onboarding'"))
            await conn.execute(
                text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username=:n)"), {"n": _ADMIN}
            )
            await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _ADMIN})
        await drop_data_table(dc, build_data_table(DEMO_CODE, ["title"]))
    finally:
        await pyp.dispose()
        await dc.dispose()
