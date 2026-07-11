"""推送组件写操作集成测试（需 PG）：建（草稿）→ 状态流转 → 发布签名门；凭证 KEK 加密不落明文。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import PushComponent
from payipa.db.settings import get_settings
from pyp_server.main import create_app
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "pcm-test-component"
_CODE = "def push(ctx):\n    return {'sent': len(ctx.rows)}\n"


async def _cleanup() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(PushComponent.__table__).where(PushComponent.name == _NAME))
    await engine.dispose()


async def _row(cid: int) -> tuple:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        r = (
            await conn.execute(
                select(
                    PushComponent.status, PushComponent.signature, PushComponent.target_creds, PushComponent.code
                ).where(PushComponent.id == cid)
            )
        ).one()
    await engine.dispose()
    return r.status, r.signature, r.target_creds, r.code


def test_component_create_status_publish(require_pg: None) -> None:
    asyncio.run(_cleanup())
    try:
        with TestClient(create_app()) as client:
            r = client.post(
                "/api/push/components",
                json={
                    "name": _NAME,
                    "code": _CODE,
                    "allow_domains": ["example.com"],
                    "target_creds": {"token": "s3cr3t"},
                },
            )
            assert r.status_code == 200, r.text
            cid = r.json()["id"]
            assert r.json()["version"] == 1

            status, sig, creds, code = asyncio.run(_row(cid))
            assert status == "draft" and sig is None and code == _CODE
            assert creds and "s3cr3t" not in str(creds)  # 凭证 KEK 密文，不落明文

            # draft → testing
            assert client.post(f"/api/push/components/{cid}/status", json={"status": "testing"}).status_code == 200
            assert asyncio.run(_row(cid))[0] == "testing"

            # 发布 → active + 签名
            p = client.post(f"/api/push/components/{cid}/publish")
            assert p.status_code == 200 and p.json()["signed"] is True
            st, sg, _, _ = asyncio.run(_row(cid))
            assert st == "active" and sg

            # 不存在 → 404；非法状态 → 422
            assert client.post("/api/push/components/99999999/publish").status_code == 404
            assert client.post(f"/api/push/components/{cid}/status", json={"status": "active"}).status_code == 422
    finally:
        asyncio.run(_cleanup())
