"""M1 打磨 · raw GC 集成测试（需 PG）：过期 local 工件被删（对象 + 登记行），未过期保留。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from payipa.db.data_center import Artifact
from payipa.db.settings import get_settings
from payipa.storage.gc import gc_expired_artifacts
from payipa.storage.local import LocalBackend
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine


def test_gc_removes_expired(require_pg: None, tmp_path) -> None:
    async def main() -> None:
        engine = create_async_engine(get_settings().async_url("data_center"))
        backend = LocalBackend(tmp_path)
        expired_key = "gctest/raw/1/old.zst"
        keep_key = "gctest/raw/1/new.zst"
        await backend.save_bytes(expired_key, b"old")
        await backend.save_bytes(keep_key, b"new")
        past = datetime.now(UTC) - timedelta(days=1)
        future = datetime.now(UTC) + timedelta(days=7)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(Artifact.__table__).where(Artifact.object_key.like("gctest/%")))
                for key, exp in ((expired_key, past), (keep_key, future)):
                    await conn.execute(
                        pg_insert(Artifact.__table__).values(
                            bucket="local",
                            object_key=key,
                            storage_backend="local",
                            size=3,
                            status="uploaded",
                            source_id="gctest",
                            expires_at=exp,
                        )
                    )

            removed = await gc_expired_artifacts(engine, backend)
            assert removed >= 1

            assert not (tmp_path / expired_key).exists()  # 过期对象已删
            assert (tmp_path / keep_key).exists()  # 未过期保留

            async with engine.begin() as conn:
                remaining = (
                    (await conn.execute(select(Artifact.object_key).where(Artifact.object_key.like("gctest/%"))))
                    .scalars()
                    .all()
                )
            assert remaining == [keep_key]  # 过期登记行已删、未过期保留
        finally:
            async with engine.begin() as conn:
                await conn.execute(delete(Artifact.__table__).where(Artifact.object_key.like("gctest/%")))
            await engine.dispose()

    asyncio.run(main())
