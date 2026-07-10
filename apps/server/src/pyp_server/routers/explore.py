"""查询与查看页（03）。/api/data/{source} = Tabulator remote 后端；/data/{source} = SSR 查看页；
/api/query/sql = SQL 窗口（M5，四件套 + `sql_query` 权限 + 每次执行记审计）。"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.crawl.ingest import build_data_table
from payipa.db.engine import get_engine
from payipa.explore.query import query_data
from payipa.explore.sqlgateway import SqlWindowError, run_sql_window
from payipa.security.audit import record_audit_best_effort
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from starlette.datastructures import QueryParams

from pyp_server.auth import get_current_user, require_perm
from pyp_server.settings import get_server_settings

router = APIRouter(tags=["explore"])

_BRACKET = re.compile(r"(\w+)\[(\d+)\]\[(\w+)\]")


def _collect(qp: QueryParams, prefix: str) -> list[dict]:
    """解析 Tabulator 的 sort[0][field] / filter[0][type] 形式为有序 dict 列表。"""
    buckets: dict[int, dict] = {}
    for key, value in qp.multi_items():
        m = _BRACKET.match(key)
        if m and m.group(1) == prefix:
            buckets.setdefault(int(m.group(2)), {})[m.group(3)] = value
    return [buckets[i] for i in sorted(buckets)]


def _parse_tabulator(qp: QueryParams) -> tuple[int, int, list[dict], list[dict]]:
    page = int(qp.get("page") or 1)
    size = int(qp.get("size") or 50)
    return page, size, _collect(qp, "sort"), _collect(qp, "filter")


@router.get(
    "/api/data/{source}",
    summary="查询数据源产出（Tabulator remote：filter/sort/page）",
    dependencies=[Depends(require_perm("data.read"))],
)
async def get_data(source: str, request: Request) -> dict:
    page, size, sorters, filters = _parse_tabulator(request.query_params)
    table = build_data_table(source)  # 查询无需生成列，基础列即可
    try:
        return await query_data(
            get_engine("data_center"), table, page=page, size=size, sorters=sorters, filters=filters
        )
    except ProgrammingError:  # 表尚不存在（未采集过）
        return {"last_page": 1, "data": [], "total": 0}


class SqlWindowRequest(BaseModel):
    db: Literal["data_center", "business"] = Field("data_center", description="目标库（绝不含 pyp，03 定案）")
    sql: str = Field(..., description="查询 SQL；不写分页（包装层持有 LIMIT/OFFSET），尾部分号可省")
    limit: int = Field(100, ge=1, description="返回行数（受 sql_window_max_rows 硬顶）")
    offset: int = Field(0, ge=0, description="偏移")


class SqlWindowResponse(BaseModel):
    columns: list[str] = Field(..., description="列名（可重复，行按位置对应）")
    rows: list[list] = Field(..., description="行数据（二维数组）")
    row_count: int = Field(..., description="本次返回行数")
    truncated: bool = Field(..., description="是否被行数封顶截断（全量走异步导出）")
    elapsed_ms: int = Field(..., description="执行耗时（毫秒）")


@router.post(
    "/api/query/sql",
    response_model=SqlWindowResponse,
    summary="SQL 窗口（特权直查：四件套守护 + sql_query 权限 + 审计）",
    dependencies=[Depends(require_perm("sql_query"))],
)
async def sql_window(body: SqlWindowRequest, request: Request) -> SqlWindowResponse:
    user = await get_current_user(request)  # 审计 actor（闸门关时可为 None）
    settings = get_server_settings()
    out: dict | None = None
    error: str | None = None
    try:
        out = await run_sql_window(
            body.db,
            body.sql,
            limit=body.limit,
            offset=body.offset,
            timeout_ms=settings.sql_window_timeout_ms,
            max_rows=settings.sql_window_max_rows,
        )
    except SqlWindowError as exc:
        error = str(exc)
    # 成败都记审计（03 §2.2 加分项；best-effort：审计不可用不阻断查询本身）
    await record_audit_best_effort(
        get_engine("pyp"),
        action="sql.query",
        actor_id=int(user["id"]) if user else None,
        object_type="db",
        object_id=body.db,
        after={
            "sql": body.sql[:2000],
            "ok": error is None,
            "rows": out["row_count"] if out else 0,
            "ms": out["elapsed_ms"] if out else None,
            "error": error,
        },
        source="api",
    )
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    assert out is not None
    return SqlWindowResponse(**out)


@router.get("/data/{source}", response_class=HTMLResponse, summary="数据查看页（SSR + Tabulator remote）")
async def data_page(source: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "data.html", {"source": source, "user": user, "active": "sources"}
    )
