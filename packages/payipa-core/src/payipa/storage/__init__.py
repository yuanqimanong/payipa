"""storage —— 存储抽象：S3Backend(M5) / LocalBackend(M1 兜底，via jbutils.s3 直传随 M5)。

M0 占位已由本包替代。M1：LocalBackend + zstd raw 存档 + 工件登记（永久 object_key 入 artifacts 表）。
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from payipa_contracts import ArtifactRef
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payipa.db.data_center import Artifact
from payipa.db.engine import get_sessionmaker
from payipa.db.settings import Settings, get_settings
from payipa.storage.base import StorageBackend
from payipa.storage.gc import gc_expired_artifacts
from payipa.storage.keys import file_object_key, raw_object_key, url_fingerprint
from payipa.storage.local import LocalBackend

__all__ = [
    "LocalBackend",
    "StorageBackend",
    "build_storage",
    "file_object_key",
    "gc_expired_artifacts",
    "get_storage",
    "raw_object_key",
    "record_artifact",
    "url_fingerprint",
]


def build_storage(settings: Settings | None = None) -> StorageBackend:
    """按配置构建存储后端。M1：本地兜底；配置 S3 后走 S3Backend（M5）。"""
    settings = settings or get_settings()
    # M5：if settings.s3_endpoint: return S3Backend(...)
    return LocalBackend(settings.data_root, min_free_bytes=settings.min_free_mb * 1024 * 1024)


@lru_cache
def get_storage() -> StorageBackend:
    return build_storage()


async def record_artifact(
    ref: ArtifactRef,
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
    agent_id: str | None = None,
    source_id: str | None = None,
    owner_id: int | None = None,
    expires_at: datetime | None = None,
) -> None:
    """把永久 object_key 及关联键登记进 data_center.artifacts（按 object_key 幂等）。"""
    sessionmaker = get_sessionmaker("data_center")
    stmt = (
        pg_insert(Artifact.__table__)
        .values(
            bucket=ref.bucket,
            object_key=ref.object_key,
            storage_backend=ref.backend.value,
            size=ref.size,
            sha256=ref.sha256,
            content_type=ref.content_type,
            status="uploaded",
            task_id=task_id,
            attempt_id=attempt_id,
            agent_id=agent_id,
            source_id=source_id,
            owner_id=owner_id,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["object_key"])
    )
    async with sessionmaker() as session:
        await session.execute(stmt)
        await session.commit()
