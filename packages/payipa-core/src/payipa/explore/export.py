"""数据源产出的流式导出（CSV / JSONL）。

按 id 键集翻页（keyset）分块拉取，避免大表 OFFSET 退化与一次性载入内存；每行拍平为
系统列（id/created_at/state）+ 用户字段（fields JSONB 展开）。供 server 以流式响应下发下载。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Sequence

from sqlalchemy import Table, asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.explore.query import _apply_filters

_SYSTEM_COLS = ("id", "created_at", "state")


def _flatten(row) -> dict:
    """(id, created_at, state, fields) → 平铺 dict（系统列 + 用户字段）。"""
    rid, created_at, state, fields = row
    out: dict = {"id": rid, "created_at": created_at.isoformat() if created_at else None, "state": state}
    if isinstance(fields, dict):
        out.update(fields)
    return out


async def iter_rows(
    engine: AsyncEngine,
    table: Table,
    *,
    filters: Sequence[dict] | None = None,
    chunk: int = 1000,
) -> AsyncIterator[dict]:
    """按 id 升序键集翻页，逐行产出平铺 dict。filters 复用 Tabulator 过滤形状。"""
    after = 0
    while True:
        stmt = select(table.c.id, table.c.created_at, table.c.state, table.c.fields)
        stmt = _apply_filters(stmt, table, filters or [])
        stmt = stmt.where(table.c.id > after).order_by(asc(table.c.id)).limit(chunk)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        if not rows:
            return
        for row in rows:
            yield _flatten(row)
        after = rows[-1][0]
        if len(rows) < chunk:
            return


async def stream_jsonl(
    engine: AsyncEngine, table: Table, *, filters: Sequence[dict] | None = None
) -> AsyncIterator[str]:
    """每行一个 JSON 对象（JSONL）——无列对齐问题，任意字段集都稳。"""
    async for r in iter_rows(engine, table, filters=filters):
        yield json.dumps(r, ensure_ascii=False, default=str) + "\n"


async def stream_csv(
    engine: AsyncEngine,
    table: Table,
    *,
    field_names: Sequence[str],
    filters: Sequence[dict] | None = None,
) -> AsyncIterator[str]:
    """CSV：列 = 系统列 + 规则声明的字段名（稳定顺序）；带 UTF-8 BOM 便于 Excel 正确识别中文。"""
    columns = [*_SYSTEM_COLS, *field_names]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    yield "﻿" + buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    async for r in iter_rows(engine, table, filters=filters):
        writer.writerow(r)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
