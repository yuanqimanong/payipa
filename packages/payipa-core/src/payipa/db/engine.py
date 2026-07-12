"""异步引擎/会话工厂（懒建：不连库即可 import，server 空壳可起）。

引擎按库缓存；``create_async_engine`` 采用惰性连接池——创建不发起连接，首次执行 SQL 才连。
故主控启动（/healthz、/docs）无需活 DB。
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from payipa.db.settings import DbKey, get_settings


@lru_cache
def get_engine(db: DbKey) -> AsyncEngine:
    """返回指定库的 async 引擎（进程内缓存，懒建）。"""
    settings = get_settings()
    pool_kw = {"poolclass": NullPool} if settings.db_null_pool else {"pool_pre_ping": True}
    return create_async_engine(settings.async_url(db), future=True, **pool_kw)


@lru_cache
def get_sessionmaker(db: DbKey) -> async_sessionmaker:
    """返回指定库的 async_sessionmaker。"""
    return async_sessionmaker(bind=get_engine(db), expire_on_commit=False)
