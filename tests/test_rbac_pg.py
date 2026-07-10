"""M5 RBAC 集成测试（需 PG）：权限矩阵解析（角色∪直授、超级用户通配）+ require_perm 闸门 401/403/放行 + 开关直通。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.security.rbac import (
    assign_role,
    effective_permissions,
    has_permission,
    make_superuser,
    seed_default_rbac,
)
from pyp_server.auth import COOKIE_NAME, create_session
from pyp_server.main import app
from pyp_server.settings import get_server_settings
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_USERS = ("rbac-op", "rbac-nobody", "rbac-super")


def test_rbac_matrix_and_gate(require_pg: None) -> None:
    async def seed() -> dict[str, int]:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await seed_default_rbac(pyp)
            ids: dict[str, int] = {}
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'rbac-%')")
                )
                await conn.execute(text("DELETE FROM users WHERE username LIKE 'rbac-%'"))
                for name in _USERS:
                    uid = (
                        await conn.execute(
                            pg_insert(User.__table__)
                            .values(username=name, password_hash="x", status="active")
                            .returning(User.id)
                        )
                    ).scalar_one()
                    ids[name] = int(uid)
            await assign_role(pyp, ids["rbac-op"], "运营")
            await make_superuser(pyp, "rbac-super")

            # 矩阵（core 级）：运营有 monitor.read/push.enqueue、无 users.manage
            op = await effective_permissions(pyp, ids["rbac-op"])
            assert "push.enqueue" in op and "monitor.read" in op and "users.manage" not in op
            assert has_permission(op, "push.enqueue") and not has_permission(op, "users.manage")
            # 超级用户：通配 * 放行一切
            su = await effective_permissions(pyp, ids["rbac-super"])
            assert "*" in su and has_permission(su, "users.manage") and has_permission(su, "anything.at.all")
            # 无角色用户：空集
            assert await effective_permissions(pyp, ids["rbac-nobody"]) == set()
            return ids
        finally:
            await pyp.dispose()

    async def cleanup() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'rbac-%')")
                )
                await conn.execute(text("DELETE FROM users WHERE username LIKE 'rbac-%'"))
        finally:
            await pyp.dispose()

    ids = asyncio.run(seed())
    settings = get_server_settings()
    settings.rbac_enabled = True  # 本用例内开启闸门
    try:
        with TestClient(app) as client:
            # ① 未登录 → 401
            assert client.get("/api/monitor/queue").status_code == 401
            # ② 无权限用户 → 403
            client.cookies.set(COOKIE_NAME, create_session(ids["rbac-nobody"], "rbac-nobody"))
            assert client.get("/api/monitor/queue").status_code == 403
            # ③ 运营（有 monitor.read）→ 200
            client.cookies.set(COOKIE_NAME, create_session(ids["rbac-op"], "rbac-op"))
            assert client.get("/api/monitor/queue").status_code == 200
            # ④ 运营无 nodes.read → /api/agents 403
            assert client.get("/api/agents").status_code == 403
            # ④b 采集数据本体 /api/data/*：无 data.read → 403；运营（有）→ 过闸（表不存在回空集 200）
            client.cookies.set(COOKIE_NAME, create_session(ids["rbac-nobody"], "rbac-nobody"))
            assert client.get("/api/data/rbac_no_such_source").status_code == 403
            client.cookies.set(COOKIE_NAME, create_session(ids["rbac-op"], "rbac-op"))
            resp = client.get("/api/data/rbac_no_such_source")
            assert resp.status_code == 200 and resp.json()["data"] == []
            # ④c SSR 建源提交（实际触发采集，与 run API 同效）：运营无 sources.write → 403 渲染表单错误
            assert client.post("/sources/create", data={"name": "x"}).status_code == 403
            # ⑤ 超级用户 → 处处放行（nodes.read 亦通）
            client.cookies.set(COOKIE_NAME, create_session(ids["rbac-super"], "rbac-super"))
            assert client.get("/api/agents").status_code == 200
            # ⑥ 闸门关 → 直通（无会话也 200，保持现网行为）
            settings.rbac_enabled = False
            client.cookies.clear()
            assert client.get("/api/monitor/queue").status_code == 200
    finally:
        settings.rbac_enabled = False
        asyncio.run(cleanup())
