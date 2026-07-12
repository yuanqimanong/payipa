"""查询服务：Tabulator remote 参数（filter/sort/page）→ SQL → {last_page, data, total}。

按数据源单表查（03 定案）。默认最新排序走 (created_at, id)；UI 翻页 offset + 深度上限，
导出/对外 API 用 keyset 流式（M2/M4）。用户字段走 JSONB ->>，勾索引字段可命中生成列。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table, asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

MAX_PAGE_SIZE = 200
DEFAULT_MAX_DEPTH = 10_000  # UI 可浏览深度上限（超出引导筛选/导出，03 定案）


def _col(table: Table, field: str):
    """系统列直取；用户字段走 fields JSONB ->>。"""
    if field in table.c:
        return table.c[field]
    return table.c.fields[field].astext


def _like_escape(value: str) -> str:
    """转义 LIKE 通配符：使用户输入的 % 和 _ 按字面匹配（否则搜索 '50%' 会命中任意串，是可见的过滤不准确）。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(stmt, table: Table, filters: Sequence[dict]):
    for f in filters:
        field = f.get("field")
        value = f.get("value")
        ftype = f.get("type", "like")
        if not field or value in (None, ""):
            continue
        col = _col(table, field)
        if ftype in ("like", "ilike"):
            stmt = stmt.where(col.ilike(f"%{_like_escape(str(value))}%", escape="\\"))
        elif ftype in ("=", "eq"):
            stmt = stmt.where(col == value)
        elif ftype in ("!=", "ne"):
            stmt = stmt.where(col != value)
        # 其余算子（>, <, in, starts…）M2 扩展
    return stmt


async def query_data(
    engine: AsyncEngine,
    table: Table,
    *,
    page: int = 1,
    size: int = 50,
    sorters: Sequence[dict] | None = None,
    filters: Sequence[dict] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Tabulator remote 后端：返回 {last_page, data, total}。"""
    sorters = sorters or []
    filters = filters or []
    page = max(1, page)
    size = max(1, min(size, MAX_PAGE_SIZE))

    base = select(table.c.id, table.c.created_at, table.c.state, table.c.fields)
    base = _apply_filters(base, table, filters)

    order = []
    for s in sorters:
        field = s.get("field")
        if not field:
            continue
        col = _col(table, field)
        order.append(desc(col) if s.get("dir", "desc") == "desc" else asc(col))
    if not order:
        order = [desc(table.c.created_at), desc(table.c.id)]  # 默认最新

    count_stmt = _apply_filters(select(func.count()).select_from(table), table, filters)
    offset = (page - 1) * size
    async with engine.connect() as conn:
        total = (await conn.execute(count_stmt)).scalar() or 0
        rows = (await conn.execute(base.order_by(*order).limit(size).offset(offset))).all()

    data: list[dict[str, Any]] = []
    for row_id, created, state, fields in rows:
        record: dict[str, Any] = {
            "id": row_id,
            "created_at": created.isoformat() if created else None,
            "state": state,
        }
        if isinstance(fields, dict):
            record.update(fields)
        data.append(record)

    capped = min(total, max_depth)
    return {"last_page": max(1, math.ceil(capped / size)), "data": data, "total": total}
