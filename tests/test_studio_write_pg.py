"""组装写操作集成测试（需 PG）：状态流转 draft↔testing + 发布签名门。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import Assembly
from payipa.db.settings import get_settings
from payipa.studio.store import AssemblyStore
from pyp_server.main import create_app
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "studio-write-test"


async def _seed() -> int:
    engine = create_async_engine(get_settings().async_url("pyp"))
    try:
        async with engine.begin() as conn:
            await conn.execute(delete(Assembly.__table__).where(Assembly.name == _NAME))
        aid, _h, _v = await AssemblyStore(engine).put(
            name=_NAME, product_code="studiowr", script_ref="ref://studio-write"
        )
        return aid
    finally:
        await engine.dispose()


async def _row(aid: int) -> tuple[str, str | None]:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        r = (await conn.execute(select(Assembly.status, Assembly.signature).where(Assembly.id == aid))).one()
    await engine.dispose()
    return r.status, r.signature


async def _cleanup() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(Assembly.__table__).where(Assembly.name == _NAME))
    await engine.dispose()


def test_assembly_status_and_publish(require_pg: None) -> None:
    aid = asyncio.run(_seed())
    try:
        with TestClient(create_app()) as client:
            assert asyncio.run(_row(aid)) == ("draft", None)

            # draft → testing
            r = client.post(f"/api/assemblies/{aid}/status", json={"status": "testing"})
            assert r.status_code == 200 and r.json()["status"] == "testing"
            assert asyncio.run(_row(aid))[0] == "testing"

            # 发布 → active + 写签名
            p = client.post(f"/api/assemblies/{aid}/publish")
            assert p.status_code == 200 and p.json()["signed"] is True
            status, sig = asyncio.run(_row(aid))
            assert status == "active" and sig  # 已签名

            # 发布对不存在的组装 → 404；非法状态 → 422
            assert client.post("/api/assemblies/99999999/publish").status_code == 404
            assert client.post(f"/api/assemblies/{aid}/status", json={"status": "active"}).status_code == 422
    finally:
        asyncio.run(_cleanup())


def test_publish_requires_perm_when_rbac_on(require_pg: None, monkeypatch) -> None:
    """RBAC 开 + 无角色 → 发布 403（assemblies.publish 门控）。"""
    from payipa.db.pyp import User
    from pyp_server.auth import COOKIE_NAME, create_session, hash_password
    from pyp_server.settings import get_server_settings
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    who = "studio-nobody"

    async def seed_user() -> int:
        engine = create_async_engine(get_settings().async_url("pyp"))
        async with engine.begin() as conn:
            await conn.execute(delete(User.__table__).where(User.username == who))
            uid = (
                await conn.execute(
                    pg_insert(User.__table__)
                    .values(username=who, password_hash=hash_password("pw-studio-nobody"), status="active")
                    .returning(User.id)
                )
            ).scalar_one()
        await engine.dispose()
        return int(uid)

    async def cleanup_user() -> None:
        engine = create_async_engine(get_settings().async_url("pyp"))
        async with engine.begin() as conn:
            await conn.execute(delete(User.__table__).where(User.username == who))
        await engine.dispose()

    aid = asyncio.run(_seed())
    uid = asyncio.run(seed_user())
    monkeypatch.setenv("PYP_SERVER_RBAC_ENABLED", "true")
    get_server_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            client.cookies.set(COOKIE_NAME, create_session(uid, who))
            assert client.post(f"/api/assemblies/{aid}/publish").status_code == 403
    finally:
        get_server_settings.cache_clear()
        asyncio.run(cleanup_user())
        asyncio.run(_cleanup())
