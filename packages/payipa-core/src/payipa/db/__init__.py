"""payipa 持久化基座。

导入本包即注册三库全部固定表到各自 MetaData（供 Alembic target）。
运行时动态表（data_*/asm_*）不在此列——由 core 建源/组装时程序化 DDL。
"""

from __future__ import annotations

# 导入模型模块以填充各 Base 的 metadata（Alembic env.py 依赖此副作用）
from payipa.db import business, data_center, pyp  # noqa: F401
from payipa.db.base import BusinessBase, DataCenterBase, PypBase
from payipa.db.engine import get_engine, get_sessionmaker
from payipa.db.settings import Settings, get_settings

# 库键 -> MetaData（Alembic multidb 与运维脚本共用）
METADATA_BY_DB = {
    "pyp": PypBase.metadata,
    "data_center": DataCenterBase.metadata,
    "business": BusinessBase.metadata,
}

__all__ = [
    "METADATA_BY_DB",
    "BusinessBase",
    "DataCenterBase",
    "PypBase",
    "Settings",
    "get_engine",
    "get_sessionmaker",
    "get_settings",
]
