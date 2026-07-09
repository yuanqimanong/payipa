"""M3 slice-3 集成测试（需 PG）：HTTP /internal/query 网关 —— job_token 鉴权 + scope 授权 + 结构化取数。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.security.job_token import issue_job_token
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
                for i, t in enumerate(["one", "two"]):
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
        client = TestClient(app)

        # 1) 令牌 scope 含 data_m3gw → 200 + 2 行
        tok, _ = issue_job_token(secret, "job-x", tables=[f"data_{_UUID}"], lease_s=300)
        r = client.post("/internal/query", json={"source": _UUID, "limit": 50}, headers={"x-job-token": tok})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["quota"]["rows_returned"] == 2
        assert {row["fields"]["title"] for row in body["rows"]} == {"one", "two"}
        assert body["next_cursor"] is None

        # 2) 令牌 scope 不含该表 → 403（越权）
        tok2, _ = issue_job_token(secret, "job-y", tables=["data_other"], lease_s=300)
        r2 = client.post("/internal/query", json={"source": _UUID}, headers={"x-job-token": tok2})
        assert r2.status_code == 403

        # 3) 无效令牌 → 401
        r3 = client.post("/internal/query", json={"source": _UUID}, headers={"x-job-token": "garbage.token.x"})
        assert r3.status_code == 401
    finally:
        asyncio.run(cleanup())
