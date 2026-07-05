"""Alembic 环境（三库 multidb，同步 psycopg 驱动）。

一套共享版本历史，每个 revision 含 ``upgrade_<db>()`` 分函数；一次 ``upgrade head`` 迁移三库
（每库有各自的 alembic_version 表）。连接串从 pydantic-settings（.env）构建，与运行时同源。
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from payipa.db import METADATA_BY_DB
from payipa.db.settings import get_settings
from sqlalchemy import create_engine

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

db_keys = [x.strip() for x in (config.get_main_option("databases") or "").split(",") if x.strip()]
target_metadata = {k: METADATA_BY_DB[k] for k in db_keys}
settings = get_settings()


def _process_revision_directives(ctx, revision, directives) -> None:
    """autogenerate 时：若三库均无变更则不生成空迁移。"""
    cmd_opts = getattr(config, "cmd_opts", None)
    if not (cmd_opts and getattr(cmd_opts, "autogenerate", False)):
        return
    script = directives[0]
    if script.upgrade_ops_list and all(ops.is_empty() for ops in script.upgrade_ops_list):
        directives[:] = []
        logger.info("三库均无 schema 变更，跳过生成。")


def run_migrations_offline() -> None:
    for name in db_keys:
        logger.info("离线迁移库 %s", name)
        context.configure(
            url=settings.sync_url(name).render_as_string(hide_password=False),
            target_metadata=target_metadata[name],
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            upgrade_token=f"{name}_upgrades",
            downgrade_token=f"{name}_downgrades",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations(engine_name=name)


def run_migrations_online() -> None:
    engines = {name: create_engine(settings.sync_url(name)) for name in db_keys}
    conns = {}
    txns = {}
    try:
        for name, engine in engines.items():
            conns[name] = engine.connect()
            txns[name] = conns[name].begin()
        for name in db_keys:
            logger.info("在线迁移库 %s", name)
            context.configure(
                connection=conns[name],
                upgrade_token=f"{name}_upgrades",
                downgrade_token=f"{name}_downgrades",
                target_metadata=target_metadata[name],
                compare_type=True,
                process_revision_directives=_process_revision_directives,
            )
            context.run_migrations(engine_name=name)
        for txn in txns.values():
            txn.commit()
    except Exception:
        for txn in txns.values():
            txn.rollback()
        raise
    finally:
        for conn in conns.values():
            conn.close()
        for engine in engines.values():
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
