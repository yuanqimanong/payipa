"""SQLAlchemy 2.0 声明式基座：三库各一 MetaData（供 Alembic 分别 target）。

命名约定统一（Alembic 迁移可读、可自动生成稳定名）。这些是 core 的**持久化细节**，
绝不进 contracts（红线：contracts 只描述传输形状，DB schema 可与之不同）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Alembic 约束命名约定（避免匿名约束名漂移）
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class PypBase(DeclarativeBase):
    """`pyp` 平台库（系统元数据 + 操作日志）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DataCenterBase(DeclarativeBase):
    """`data_center` 采集数据库（固定表 artifacts；data_* 为运行时动态表，不建模）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BusinessBase(DeclarativeBase):
    """`business` 组装产物库（asm_* 为运行时动态表；M0 无固定表）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ── 通用列 mixin ────────────────────────────────────────────────────────────
class TimestampMixin:
    """所有表带 created_at（timestamptz，UTC 存、东八显示）。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OwnedMixin:
    """业务/资源表带 owner_id（应用层 owner 过滤）+ 预留 tenant_id（RLS 暂不启用）。"""

    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
