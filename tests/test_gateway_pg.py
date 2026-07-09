"""M3 slice-3/4 集成测试（需 PG）：HTTP /internal/query 网关 —— job_token 鉴权 + scope 授权 + 结构化取数
+ 签名不透明游标翻页 + 行数配额强制（伪造/跨作业游标拒绝）。

DB 准备/清理用独立 create_async_engine（各自 asyncio.run 事件循环）；HTTP 用 `with TestClient`（稳定 portal
事件循环，避免与建表/清理 loop 混用导致 asyncpg 跨 loop 报错）。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.security.job_token import issue_job_token
from payipa.studio.cursor import encode_cursor
from pyp_server.main import app
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m3gw"


def test_query_gateway_endpoint(require_pg: None) -> None:
    table = build_data_table(_UUID, [])

    async def seed() -> None:
        dc = create_async_engine(get_settings().async_url("data_center"))
        try:
            await drop_data_table(dc, table)
            await create_data_table(dc, table)
            async with dc.begin() as conn:
                for i, t in enumerate(["one", "two", "three", "four", "five"]):
                    await conn.execute(pg_insert(table).values(data_fingerprint=f"g{i}", state=3, fields={"title": t}))
        finally:
            await dc.dispose()

    async def cleanup() -> None:
        dc = create_async_engine(get_settings().async_url("data_center"))
        try:
            await drop_data_table(dc, table)
        finally:
            await dc.dispose()

    asyncio.run(seed())
    try:
        secret = get_settings().upload_secret
        with TestClient(app) as client:
            # 1) 令牌 scope 含 data_m3gw → 200 + 全部 5 行（limit 50 > 5，无下页）
            tok, _ = issue_job_token(secret, "job-x", tables=[f"data_{_UUID}"], lease_s=300)
            r = client.post("/internal/query", json={"source": _UUID, "limit": 50}, headers={"x-job-token": tok})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["quota"]["rows_returned"] == 5
            assert {row["fields"]["title"] for row in body["rows"]} == {"one", "two", "three", "four", "five"}
            assert body["next_cursor"] is None

            # 2) 令牌 scope 不含该表 → 403（越权）
            tok2, _ = issue_job_token(secret, "job-y", tables=["data_other"], lease_s=300)
            r2 = client.post("/internal/query", json={"source": _UUID}, headers={"x-job-token": tok2})
            assert r2.status_code == 403

            # 3) 无效令牌 → 401
            r3 = client.post("/internal/query", json={"source": _UUID}, headers={"x-job-token": "garbage.token.x"})
            assert r3.status_code == 401

            # ── slice-4：签名游标 + 行数配额 ──────────────────────────────────
            # 4) quota=3, limit=2：第一页 2 行 + 签名游标(remaining=1)；第二页被配额截为 1 行、无下页游标
            tok3, jti3 = issue_job_token(secret, "job-q", tables=[f"data_{_UUID}"], row_quota=3, lease_s=300)
            p1 = client.post(
                "/internal/query", json={"source": _UUID, "limit": 2}, headers={"x-job-token": tok3}
            ).json()
            assert p1["quota"] == {"rows_returned": 2, "quota": 3, "rows_remaining": 1}
            assert isinstance(p1["next_cursor"], str) and "." in p1["next_cursor"]  # 不透明签名游标
            p2 = client.post(
                "/internal/query",
                json={"source": _UUID, "limit": 2, "cursor_token": p1["next_cursor"]},
                headers={"x-job-token": tok3},
            ).json()
            assert p2["quota"] == {"rows_returned": 1, "quota": 3, "rows_remaining": 0}
            assert p2["next_cursor"] is None  # 配额耗尽 → 不再发游标

            # 5) 配额耗尽的游标（consumed≥quota）→ 403
            spent = encode_cursor(secret, after_id=0, consumed=3, jti=jti3, source=_UUID)
            r5 = client.post(
                "/internal/query", json={"source": _UUID, "cursor_token": spent}, headers={"x-job-token": tok3}
            )
            assert r5.status_code == 403 and "quota" in r5.text

            # 6) 篡改游标 → 400；跨作业（别的 jti 签的）游标 → 400
            r6 = client.post(
                "/internal/query",
                json={"source": _UUID, "cursor_token": p1["next_cursor"][:-3] + "xxx"},
                headers={"x-job-token": tok3},
            )
            assert r6.status_code == 400
            alien = encode_cursor(secret, after_id=0, consumed=0, jti="other-jti", source=_UUID)
            r7 = client.post(
                "/internal/query", json={"source": _UUID, "cursor_token": alien}, headers={"x-job-token": tok3}
            )
            assert r7.status_code == 400
    finally:
        asyncio.run(cleanup())
