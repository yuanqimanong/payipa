"""SQL 窗口（03 §2.2 四件套）：受信角色（管理员/技术）专属的直查特权工具。

四件套守护（缺一不可）：
①**子查询包装分页**——用户 SQL 进 FROM 子查询（``SELECT * FROM (<SQL>) AS q LIMIT … OFFSET …``），
  多语句注入 / 顶层 DML / 数据修改型 CTE 均天然变语法错误或被 PG 拒绝；
②**只读防线**——配置了 ``PG_RO_USER`` 则经独立只读角色连接（``setup-sql-readonly`` 建角色授权），
  且无论哪个角色**恒定**事务级 ``READ ONLY``（双保险，写操作数据库层面不可能）；
③**语句超时**——``SET LOCAL statement_timeout``，慢查询自动杀；
④**行数封顶**——LIMIT 取 min(请求, 硬顶)，超顶标记 truncated（全量走异步导出，后续 Exporter）。

仅 ``data_center`` / ``business`` 两库（**绝不含 pyp**，03 定案）；每次执行由 server 侧记审计。
窗口显式**不做 owner/行级隔离**（03 定案：特权工具，权限码 ``sql_query`` 把门）。
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from payipa.db.engine import get_engine
from payipa.db.settings import get_settings

WindowDb = Literal["data_center", "business"]
WINDOW_DBS: tuple[str, ...] = ("data_center", "business")

DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_MAX_ROWS = 10_000

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")  # PG 角色名白名单（防 DDL 注入）

_ro_engines: dict[str, AsyncEngine] = {}  # 只读角色引擎缓存（键=库）


class SqlWindowError(ValueError):
    """SQL 窗口执行被拒/失败（语法、越权写、超时…）；message 面向用户。"""


def _window_engine(db: WindowDb) -> AsyncEngine:
    """取窗口引擎：配了只读角色走只读 DSN，否则主引擎（事务级 READ ONLY 恒在）。"""
    ro_url = get_settings().ro_async_url(db)
    if ro_url is None:
        return get_engine(db)
    eng = _ro_engines.get(db)
    if eng is None:
        eng = _ro_engines[db] = create_async_engine(ro_url, pool_pre_ping=True, future=True)
    return eng


def _jsonable(v: Any) -> Any:
    """行值转 JSON 可编码：标量原样，datetime/date 转 ISO，其余（Decimal/UUID/Range…）转 str。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


async def run_sql_window(
    db: WindowDb,
    sql: str,
    *,
    limit: int = 100,
    offset: int = 0,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_rows: int = DEFAULT_MAX_ROWS,
    engine: AsyncEngine | None = None,
) -> dict[str, Any]:
    """执行一次窗口查询，返回 ``{columns, rows, row_count, truncated, elapsed_ms}``。

    ``rows`` 为按 columns 顺序的二维数组（列名可重复，不折成 dict）。失败抛 SqlWindowError。
    """
    if db not in WINDOW_DBS:  # 防御：Literal 之外的运行时传参（红线：绝不碰 pyp）
        raise SqlWindowError(f"SQL 窗口仅限 {WINDOW_DBS}，不允许 {db!r}")
    body = sql.strip().rstrip(";").strip()
    if not body:
        raise SqlWindowError("SQL 不能为空")
    eff_limit = max(1, min(int(limit), int(max_rows)))
    # LIMIT 多取 1 行探测截断；offset 由包装层持有，用户 SQL 不写分页（03 定案）
    wrapped = f"SELECT * FROM (\n{body}\n) AS q LIMIT {eff_limit + 1} OFFSET {max(0, int(offset))}"
    eng = engine or _window_engine(db)
    t0 = time.perf_counter()
    try:
        async with eng.connect() as conn, conn.begin():
            # exec_driver_sql：原样直达驱动，用户 SQL 里的 :name / % 不做绑定参数解析
            await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            await conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            result = await conn.exec_driver_sql(wrapped)
            columns = list(result.keys())
            fetched = result.fetchall()
    except DBAPIError as exc:
        raise SqlWindowError(str(getattr(exc, "orig", exc)).strip()) from exc
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    truncated = len(fetched) > eff_limit
    rows = [[_jsonable(v) for v in row] for row in fetched[:eff_limit]]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }


async def setup_sql_readonly(role: str, password: str) -> str:
    """建共享只读角色并授权（幂等，管理员运维命令用）。

    对 data_center/business 两库：GRANT CONNECT/USAGE + 存量表 SELECT + 未来表默认 SELECT
    （ALTER DEFAULT PRIVILEGES 随建表角色，动态 data_*/asm_* 表自动被覆盖）。**不触碰 pyp 库**。
    """
    if not _IDENT.match(role):
        raise ValueError(f"角色名不合法（^[a-z_][a-z0-9_]*$）：{role!r}")
    pwd = password.replace("'", "''")
    eng_dc = get_engine("data_center")
    async with eng_dc.begin() as conn:
        exists = (await conn.exec_driver_sql(f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'")).first() is not None
        if exists:
            await conn.exec_driver_sql(f"ALTER ROLE {role} WITH LOGIN PASSWORD '{pwd}'")
        else:
            await conn.exec_driver_sql(f"CREATE ROLE {role} WITH LOGIN PASSWORD '{pwd}'")
    settings = get_settings()
    keys: tuple[WindowDb, ...] = ("data_center", "business")
    for key in keys:
        eng = get_engine(key)
        dbname = settings._db_name(key)  # 同包内部约定（conftest 亦如此用）
        async with eng.begin() as conn:
            await conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{dbname}" TO {role}')
            await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            await conn.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
            await conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}")
    return f"只读角色 '{role}' 就绪（data_center/business 存量+未来表 SELECT；.env 配 PG_RO_USER/PG_RO_PASSWORD 启用）"
