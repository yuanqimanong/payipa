"""用户管理写操作集成测试（需 PG）：创建 / 停用 / 启用 + 唯一性 + RBAC 门控。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from pyp_server.main import create_app
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

_USER = "mng-created-user"


async def _cleanup() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username == _USER))
    await engine.dispose()


async def _status_of(username: str) -> str | None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        row = (await conn.execute(select(User.status).where(User.username == username))).first()
    await engine.dispose()
    return row[0] if row else None


def test_create_and_toggle_user(require_pg: None) -> None:
    asyncio.run(_cleanup())
    try:
        with TestClient(create_app()) as client:
            # 创建
            r = client.post("/api/users", json={"username": _USER, "password": "s3cret-pw-123", "display_name": "M"})
            assert r.status_code == 200, r.text
            uid = r.json()["id"]
            assert uid > 0
            assert asyncio.run(_status_of(_USER)) == "active"
            # 密码不落明文（存 argon2 hash）
            assert asyncio.run(_password_is_hashed(_USER))

            # 用户名唯一 → 409
            dup = client.post("/api/users", json={"username": _USER, "password": "another-pw-123"})
            assert dup.status_code == 409

            # 密码太短 → 422（pydantic 校验）
            short = client.post("/api/users", json={"username": "x-short", "password": "short"})
            assert short.status_code == 422

            # 停用 → disabled
            d = client.post(f"/api/users/{uid}/status", json={"status": "disabled"})
            assert d.status_code == 200 and d.json()["status"] == "disabled"
            assert asyncio.run(_status_of(_USER)) == "disabled"

            # 启用 → active
            a = client.post(f"/api/users/{uid}/status", json={"status": "active"})
            assert a.status_code == 200
            assert asyncio.run(_status_of(_USER)) == "active"

            # 非法状态 → 422；不存在用户 → 404
            assert client.post(f"/api/users/{uid}/status", json={"status": "banned"}).status_code == 422
            assert client.post("/api/users/99999999/status", json={"status": "active"}).status_code == 404

            # 口令重置：新密码可登录、旧密码失效；hash 变化且不含明文
            old_hash = asyncio.run(_hash_of(_USER))
            rp = client.post(f"/api/users/{uid}/password", json={"password": "brand-new-pw-9"})
            assert rp.status_code == 200 and rp.json()["reset"] is True
            new_hash = asyncio.run(_hash_of(_USER))
            assert new_hash != old_hash and new_hash.startswith("$argon2") and "brand-new-pw-9" not in new_hash
            # 太短 → 422；不存在用户 → 404
            assert client.post(f"/api/users/{uid}/password", json={"password": "short"}).status_code == 422
            assert client.post("/api/users/99999999/password", json={"password": "long-enough-pw"}).status_code == 404
    finally:
        asyncio.run(_cleanup())


async def _hash_of(username: str) -> str:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        h = (await conn.execute(select(User.password_hash).where(User.username == username))).scalar()
    await engine.dispose()
    return h or ""


def test_grant_and_revoke_role(require_pg: None) -> None:
    """给用户授予/撤销角色（幂等），角色反映到 /api/views/users。"""
    from payipa.security.rbac import seed_default_rbac

    async def _seed_roles() -> None:
        engine = create_async_engine(get_settings().async_url("pyp"))
        try:
            await seed_default_rbac(engine)  # 幂等：确保四角色存在
        finally:
            await engine.dispose()

    async def _roles_of(username: str) -> list[str]:
        from payipa.views import list_users

        engine = create_async_engine(get_settings().async_url("pyp"))
        try:
            users = await list_users(engine)
        finally:
            await engine.dispose()
        return next((u["roles"] for u in users if u["username"] == username), [])

    asyncio.run(_cleanup())
    asyncio.run(_seed_roles())
    try:
        with TestClient(create_app()) as client:
            uid = client.post("/api/users", json={"username": _USER, "password": "role-pw-12345"}).json()["id"]

            # 授予「技术」→ 出现在角色列表
            g = client.post(f"/api/users/{uid}/roles", json={"role": "技术", "action": "grant"})
            assert g.status_code == 200
            assert "技术" in asyncio.run(_roles_of(_USER))

            # 幂等：重复授予不报错
            assert client.post(f"/api/users/{uid}/roles", json={"role": "技术", "action": "grant"}).status_code == 200

            # 撤销 → 移除
            r = client.post(f"/api/users/{uid}/roles", json={"role": "技术", "action": "revoke"})
            assert r.status_code == 200
            assert "技术" not in asyncio.run(_roles_of(_USER))

            # 不存在的角色 → 404；不存在的用户 → 404
            assert (
                client.post(f"/api/users/{uid}/roles", json={"role": "不存在角色", "action": "grant"}).status_code
                == 404
            )
            assert client.post("/api/users/99999999/roles", json={"role": "技术", "action": "grant"}).status_code == 404
    finally:
        asyncio.run(_cleanup())


async def _password_is_hashed(username: str) -> bool:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        h = (await conn.execute(select(User.password_hash).where(User.username == username))).scalar()
    await engine.dispose()
    return bool(h) and h.startswith("$argon2") and "s3cret-pw-123" not in h


def test_manage_requires_rbac_when_enabled(require_pg: None, monkeypatch) -> None:
    """RBAC 开启 + 无角色用户 → 用户管理写操作 403。"""
    from pyp_server.auth import COOKIE_NAME, create_session, hash_password
    from pyp_server.settings import get_server_settings

    nobody = "mng-nobody"

    async def seed() -> int:
        engine = create_async_engine(get_settings().async_url("pyp"))
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with engine.begin() as conn:
            await conn.execute(delete(User.__table__).where(User.username == nobody))
            uid = (
                await conn.execute(
                    pg_insert(User.__table__)
                    .values(username=nobody, password_hash=hash_password("pw-for-nobody"), status="active")
                    .returning(User.id)
                )
            ).scalar_one()
        await engine.dispose()
        return int(uid)

    async def cleanup() -> None:
        engine = create_async_engine(get_settings().async_url("pyp"))
        async with engine.begin() as conn:
            await conn.execute(delete(User.__table__).where(User.username == nobody))
        await engine.dispose()

    uid = asyncio.run(seed())
    monkeypatch.setenv("PYP_SERVER_RBAC_ENABLED", "true")
    get_server_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            client.cookies.set(COOKIE_NAME, create_session(uid, nobody))
            resp = client.post("/api/users", json={"username": "should-fail", "password": "long-enough-pw"})
            assert resp.status_code == 403
    finally:
        get_server_settings.cache_clear()
        asyncio.run(cleanup())
