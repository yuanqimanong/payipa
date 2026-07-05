"""pytest 夹具：PG 集成测试在无 DB 环境（如 CI）自动跳过。"""

from __future__ import annotations

import asyncio
import os

import pytest

# 测试默认关闭后台派发环（避免它与用例抢 QUEUED 请求 / 无 PG 时刷错误日志）。
os.environ.setdefault("PYP_SERVER_DISPATCH_ENABLED", "0")


def _pg_reachable() -> bool:
    import asyncpg
    from payipa.db.settings import get_settings

    s = get_settings()

    async def _chk() -> bool:
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=s.pg_host,
                    port=s.pg_port,
                    user=s.pg_user,
                    password=s.pg_password,
                    database=s._db_name("data_center"),
                ),
                timeout=3,
            )
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(_chk())


@pytest.fixture(scope="session")
def require_pg() -> None:
    if not _pg_reachable():
        pytest.skip("PostgreSQL not reachable; skipping PG integration test")


@pytest.fixture(autouse=True)
def _reset_caches():
    """每个用例前清空进程内缓存：让 async 引擎绑定到本用例事件循环（避免跨 TestClient loop 复用）。"""
    from payipa.db.engine import get_engine, get_sessionmaker
    from payipa.db.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        from payipa.storage import get_storage

        get_storage.cache_clear()
    except Exception:
        pass
    yield
