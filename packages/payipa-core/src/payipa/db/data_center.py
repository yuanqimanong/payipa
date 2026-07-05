"""`data_center` 采集数据库：固定表 ``artifacts``。

`data_{source短码}` 表是**运行时动态表**（加法演进：系统列 + JSONB + 勾索引 STORED 生成列），
由 core 在建源时程序化 DDL，**不进 Alembic**（M1 落地）。此处只建模固定的 artifacts 表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from payipa.db.base import DataCenterBase, OwnedMixin, TimestampMixin


class Artifact(TimestampMixin, OwnedMixin, DataCenterBase):
    """工件元数据：DB 记永久 object_key（非临时 URL），关联 task/agent/source 可回溯。"""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(8), default="s3")  # s3/local
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # GC
