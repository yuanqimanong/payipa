"""模型卫生集成测试（需 PG）：DB-014 updated_at 自动刷新 + DB-012 非法状态被 CHECK 拒绝。"""

from __future__ import annotations

import asyncio

import pytest
from payipa.db.pyp import LlmModel, User
from payipa.db.settings import get_settings
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

_NAME = "hygiene-probe"


def test_updated_at_bumps_on_update(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with pyp.begin() as conn:
                mid = (
                    await conn.execute(
                        pg_insert(LlmModel.__table__).values(name=_NAME, provider="echo").returning(LlmModel.id)
                    )
                ).scalar_one()
                first = (await conn.execute(select(LlmModel.updated_at).where(LlmModel.id == mid))).scalar()
            async with pyp.begin() as conn:
                await conn.execute(text("SELECT pg_sleep(0.05)"))
                await conn.execute(update(LlmModel.__table__).where(LlmModel.id == mid).values(enabled=False))
                second = (await conn.execute(select(LlmModel.updated_at).where(LlmModel.id == mid))).scalar()
            assert second > first, "更新配置必须刷新 updated_at（DB-014）"
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM llm_models WHERE name=:n"), {"n": _NAME})
            await pyp.dispose()

    asyncio.run(main())


def test_check_rejects_bad_state(require_pg: None) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            with pytest.raises((IntegrityError, DBAPIError)):
                async with pyp.begin() as conn:
                    await conn.execute(
                        pg_insert(User.__table__).values(username=_NAME, password_hash="x", status="banana")
                    )
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM users WHERE username=:n"), {"n": _NAME})
            await pyp.dispose()

    asyncio.run(main())
