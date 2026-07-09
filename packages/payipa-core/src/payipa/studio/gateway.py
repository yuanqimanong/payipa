"""Query Gateway：受控结构化取数（红线2——用户/AI 代码不直连 DB）。

M3 首刀为**进程内**网关：把 TableQueryRequest 翻成对 data_{源} 的安全 SELECT（无 SQL 串），按 id 升序 +
keyset 翻页，AND 过滤（系统列或用户字段 fields->>名），返回行 dict。HTTP+Arrow 边界与 job_token 鉴权在
后续切片；此处先坐实「结构化查询 → 行」这条唯一取数路径的语义。
"""

from __future__ import annotations

from typing import Any

from payipa_contracts import ColumnFilter, FilterOp, KeysetCursor, QuotaMeta, TableQueryRequest
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl.ingest import build_data_table

# 可直接过滤/投影的系统列（其余列名一律当用户字段，走 fields ->> 名）
_SYSTEM_COLS = {"id", "state", "batch_id", "created_at", "updated_at", "data_fingerprint"}


def _column(table, name: str):
    """把列名解析为可比较的 SQL 表达式：系统列 → 实列；否则 → fields ->> '名'（文本）。"""
    if name in _SYSTEM_COLS and name in table.c:
        return table.c[name]
    return table.c["fields"][name].astext


def _apply(col, f: ColumnFilter):
    op = f.op
    if op == FilterOp.EQ:
        return col == f.value
    if op == FilterOp.NE:
        return col != f.value
    if op == FilterOp.GT:
        return col > f.value
    if op == FilterOp.GTE:
        return col >= f.value
    if op == FilterOp.LT:
        return col < f.value
    if op == FilterOp.LTE:
        return col <= f.value
    if op == FilterOp.IN:
        return col.in_(f.value if isinstance(f.value, list | tuple) else [f.value])
    if op == FilterOp.CONTAINS:
        return col.like(f"%{f.value}%")
    raise ValueError(f"不支持的过滤算子: {op}")


def _project(row: dict, columns: list[str] | None) -> dict:
    """投影：columns=None 返回系统列 + fields 袋；否则只挑指定列（系统列或字段名）。"""
    if columns is None:
        return {"id": row["id"], "state": row["state"], "created_at": row["created_at"], "fields": row["fields"]}
    out: dict[str, Any] = {}
    for name in columns:
        out[name] = row[name] if name in _SYSTEM_COLS else (row.get("fields") or {}).get(name)
    return out


class QueryGateway:
    """进程内网关：读某数据源 data_* 的结构化视图。"""

    async def read(
        self, engine_dc: AsyncEngine, req: TableQueryRequest
    ) -> tuple[list[dict], KeysetCursor | None, QuotaMeta]:
        """返回 (行列表, 下一页游标|None, 配额回执)。按 id 升序 + keyset；AND 过滤；多取 1 行探测是否还有下页。"""
        table = build_data_table(req.source)  # 只读无需生成列
        after = req.cursor.after_id if req.cursor else 0
        conds = [table.c["id"] > after]
        conds += [_apply(_column(table, f.column), f) for f in req.filters]
        stmt = select(table).where(and_(*conds)).order_by(table.c["id"].asc()).limit(req.limit + 1)
        async with engine_dc.connect() as conn:
            fetched = (await conn.execute(stmt)).mappings().all()
        has_more = len(fetched) > req.limit
        page = [dict(r) for r in fetched[: req.limit]]
        rows = [_project(r, req.columns) for r in page]
        nxt = KeysetCursor(after_id=page[-1]["id"]) if (has_more and page) else None
        return rows, nxt, QuotaMeta(rows_returned=len(rows))
