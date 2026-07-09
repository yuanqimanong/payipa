"""M4 slice-2 集成测试（需 PG）：对外 Dataset API —— API Key 鉴权 + scope 授权 + asm_ 产物 keyset 分页。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.settings import get_settings
from payipa.deliver.dataset import create_api_key
from payipa.studio.asm import AsmLoader, build_asm_table, create_asm_table, drop_asm_table
from pyp_server.main import app
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_PROD = "m4ds"


def test_dataset_api(require_pg: None) -> None:
    asm = build_asm_table(_PROD, [])
    key_holder: dict[str, str] = {}

    async def seed() -> None:
        biz = create_async_engine(get_settings().async_url("business"))
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await drop_asm_table(biz, asm)
            await create_asm_table(biz, asm)
            await AsmLoader(biz).upsert(asm, [{"title": f"t{i}", "n": i} for i in range(3)], fingerprint_keys=["title"])
            # 有 scope 的 key + 无 scope 的 key
            key_holder["ok"] = await create_api_key(pyp, name="m4ds-ok", datasets=[_PROD])
            key_holder["no"] = await create_api_key(pyp, name="m4ds-no", datasets=["other"])
        finally:
            await biz.dispose()
            await pyp.dispose()

    async def cleanup() -> None:
        biz = create_async_engine(get_settings().async_url("business"))
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await drop_asm_table(biz, asm)
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM api_keys WHERE name IN ('m4ds-ok','m4ds-no')"))
        finally:
            await biz.dispose()
            await pyp.dispose()

    asyncio.run(seed())
    try:
        with TestClient(app) as client:
            # 1) 有效 key + scope 命中 → 200 + 3 行（keyset limit 大，无下页）
            r = client.get(f"/api/datasets/{_PROD}?limit=50", headers={"x-api-key": key_holder["ok"]})
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["rows"]) == 3 and body["next_cursor"] is None
            assert {row["title"] for row in body["rows"]} == {"t0", "t1", "t2"}
            assert "id" in body["rows"][0]  # 系统列 + 展开的产物字段

            # 2) keyset 翻页：limit=2 → 2 行 + next_cursor；带 cursor 取剩余 1 行
            p1 = client.get(f"/api/datasets/{_PROD}?limit=2", headers={"x-api-key": key_holder["ok"]}).json()
            assert len(p1["rows"]) == 2 and isinstance(p1["next_cursor"], int)
            p2 = client.get(
                f"/api/datasets/{_PROD}?limit=2&cursor={p1['next_cursor']}", headers={"x-api-key": key_holder["ok"]}
            ).json()
            assert len(p2["rows"]) == 1 and p2["next_cursor"] is None

            # 3) 无效 key → 401
            assert client.get(f"/api/datasets/{_PROD}", headers={"x-api-key": "pyp_bogus"}).status_code == 401

            # 4) key 未授权该数据集 → 403
            assert client.get(f"/api/datasets/{_PROD}", headers={"x-api-key": key_holder["no"]}).status_code == 403

            # 5) 未知数据集（表不存在）+ 有 scope → 200 空（授权范围内但无产物）
            r5 = client.get("/api/datasets/other", headers={"x-api-key": key_holder["no"]})
            assert r5.status_code == 200 and r5.json() == {"rows": [], "next_cursor": None}
    finally:
        asyncio.run(cleanup())
