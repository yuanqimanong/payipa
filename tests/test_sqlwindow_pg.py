"""M5 SQL 窗口集成测试（需 PG）：四件套（包装分页/只读/超时/封顶）+ sql_query 闸门 + 审计落库。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.explore.sqlgateway import SqlWindowError, run_sql_window
from payipa.security.rbac import assign_role, seed_default_rbac
from pyp_server.auth import COOKIE_NAME, create_session
from pyp_server.main import app
from pyp_server.settings import get_server_settings
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_TABLE = "data_sqlwin_t"


async def _mk_engines():
    s = get_settings()
    return (
        create_async_engine(s.async_url("data_center")),
        create_async_engine(s.async_url("pyp")),
    )


async def _seed_table(dc) -> None:
    async with dc.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        await conn.execute(text(f"CREATE TABLE {_TABLE} (id bigserial PRIMARY KEY, v int NOT NULL)"))
        await conn.execute(text(f"INSERT INTO {_TABLE} (v) SELECT g FROM generate_series(1, 5) g"))


async def _drop_table(dc) -> None:
    async with dc.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))


def test_sqlwindow_four_guards(require_pg: None) -> None:
    """核心四件套（直接调 core，不过 HTTP）。"""

    async def run() -> None:
        dc, pyp = await _mk_engines()
        try:
            await _seed_table(dc)

            # ①包装分页 + 尾分号剥离：limit/offset 由包装层持有
            out = await run_sql_window(
                "data_center", f"SELECT v FROM {_TABLE} ORDER BY v;", limit=2, offset=1, engine=dc
            )
            assert out["columns"] == ["v"]
            assert out["rows"] == [[2], [3]]
            assert out["row_count"] == 2 and out["truncated"] is True  # 总 5 行 > offset1+limit2

            # ④行数封顶：limit 超过 max_rows 被压到硬顶
            out = await run_sql_window("data_center", f"SELECT v FROM {_TABLE} ORDER BY v", max_rows=3, engine=dc)
            assert out["row_count"] == 3 and out["truncated"] is True

            # 多语句注入 → 包装后语法错误，第二条语句不可能执行
            with pytest.raises(SqlWindowError):
                await run_sql_window("data_center", f"SELECT 1; DROP TABLE {_TABLE}", engine=dc)

            # 括号逃逸 + COMMIT 突破只读 + 写（经典包装突破）→ asyncpg 扩展协议单语句，直接语法错
            with pytest.raises(SqlWindowError):
                await run_sql_window(
                    "data_center",
                    f"1) AS q LIMIT 1; COMMIT; DROP TABLE {_TABLE}; SELECT * FROM (SELECT 1",
                    engine=dc,
                )
            # 注释吞尾（-- 注掉包装尾部）→ 括号不闭合，同样语法错
            with pytest.raises(SqlWindowError):
                await run_sql_window("data_center", f"SELECT 1; DROP TABLE {_TABLE}; --", engine=dc)

            # ②只读防线（恒定 READ ONLY 事务）：写函数被 PG 拒绝
            with pytest.raises(SqlWindowError, match="read-only"):
                await run_sql_window("data_center", f"SELECT setval('{_TABLE}_id_seq', 99)", engine=dc)

            # 数据修改型 CTE 在子查询里被 PG 拒绝（top level only）
            with pytest.raises(SqlWindowError):
                await run_sql_window(
                    "data_center", f"WITH d AS (DELETE FROM {_TABLE} RETURNING id) SELECT * FROM d", engine=dc
                )

            # ③语句超时：pg_sleep(10) 在 300ms 超时下被杀
            with pytest.raises(SqlWindowError, match="timeout"):
                await run_sql_window("data_center", "SELECT pg_sleep(10)", timeout_ms=300, engine=dc)

            # 上述注入/写全部未生效：表仍 5 行
            async with dc.connect() as conn:
                n = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar()
            assert n == 5

            # 空 SQL / 非白名单库拒绝
            with pytest.raises(SqlWindowError):
                await run_sql_window("data_center", "   ;  ", engine=dc)
            with pytest.raises(SqlWindowError, match="pyp"):
                await run_sql_window("pyp", "SELECT 1", engine=dc)  # type: ignore[arg-type]
        finally:
            await _drop_table(dc)
            await dc.dispose()
            await pyp.dispose()

    asyncio.run(run())


def test_sqlwindow_endpoint_gate_and_audit(require_pg: None) -> None:
    """HTTP 端点：sql_query 闸门（技术有/运营无）+ 成败均落审计。"""

    async def seed() -> dict[str, int]:
        dc, pyp = await _mk_engines()
        try:
            await _seed_table(dc)
            await seed_default_rbac(pyp)
            ids: dict[str, int] = {}
            async with pyp.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'sqlwin-%')"
                    )
                )
                await conn.execute(text("DELETE FROM users WHERE username LIKE 'sqlwin-%'"))
                for name in ("sqlwin-tech", "sqlwin-op"):
                    uid = (
                        await conn.execute(
                            pg_insert(User.__table__)
                            .values(username=name, password_hash="x", status="active")
                            .returning(User.id)
                        )
                    ).scalar_one()
                    ids[name] = int(uid)
            await assign_role(pyp, ids["sqlwin-tech"], "技术")  # 技术矩阵含 sql_query
            await assign_role(pyp, ids["sqlwin-op"], "运营")  # 运营矩阵不含
            return ids
        finally:
            await dc.dispose()
            await pyp.dispose()

    async def audit_count() -> int:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.connect() as conn:
                return int(
                    (await conn.execute(text("SELECT count(*) FROM audit_log WHERE action='sql.query'"))).scalar() or 0
                )
        finally:
            await pyp.dispose()

    async def cleanup() -> None:
        dc, pyp = await _mk_engines()
        try:
            await _drop_table(dc)
            async with pyp.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'sqlwin-%')"
                    )
                )
                await conn.execute(text("DELETE FROM users WHERE username LIKE 'sqlwin-%'"))
        finally:
            await dc.dispose()
            await pyp.dispose()

    ids = asyncio.run(seed())
    settings = get_server_settings()
    settings.rbac_enabled = True
    try:
        with TestClient(app) as client:
            payload = {"db": "data_center", "sql": f"SELECT v FROM {_TABLE} ORDER BY v", "limit": 2}
            # 未登录 → 401
            assert client.post("/api/query/sql", json=payload).status_code == 401
            # 运营（无 sql_query）→ 403
            client.cookies.set(COOKIE_NAME, create_session(ids["sqlwin-op"], "sqlwin-op"))
            assert client.post("/api/query/sql", json=payload).status_code == 403
            # 技术（有 sql_query）→ 200，形状正确
            before = asyncio.run(audit_count())
            client.cookies.set(COOKIE_NAME, create_session(ids["sqlwin-tech"], "sqlwin-tech"))
            resp = client.post("/api/query/sql", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["columns"] == ["v"] and data["rows"] == [[1], [2]] and data["truncated"] is True
            # 失败执行 → 400 且同样记审计
            assert client.post("/api/query/sql", json={**payload, "sql": "SELEC oops"}).status_code == 400
            # pyp 库在请求模型层就被拒（Literal）→ 422
            assert client.post("/api/query/sql", json={**payload, "db": "pyp"}).status_code == 422
            after = asyncio.run(audit_count())
            assert after >= before + 2  # 成功 + 失败各一条
    finally:
        settings.rbac_enabled = False
        asyncio.run(cleanup())
