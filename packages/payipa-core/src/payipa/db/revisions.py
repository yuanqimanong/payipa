"""迁移版本核验（P0-06/P0-04 共用）：脚本 head vs 三库 alembic_version。

三库共用一条线性 revision 历史（deploy/alembic 多库模板），因此三个库的
version_num 都应等于脚本目录的单一 head。readyz 与后续 pypctl doctor 共用本模块。
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine


@lru_cache
def script_head(ini_path: str = "deploy/alembic.ini") -> str | None:
    """迁移脚本目录的 head revision；目录/配置不可用返回 None（调用方按未知处理）。

    ini 的 script_location 是相对 CWD 的（须从仓库根运行）；打包部署须携带 deploy/。
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory.from_config(Config(ini_path)).get_heads()
        return heads[0] if heads else None
    except Exception:  # noqa: BLE001 —— 缺目录/坏配置一律视为未知
        return None


async def db_revision(engine: AsyncEngine) -> str | None:
    """读某库当前 alembic_version；表不存在/库不可达返回 None。"""
    try:
        async with engine.connect() as conn:
            return (await conn.exec_driver_sql("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        return None
