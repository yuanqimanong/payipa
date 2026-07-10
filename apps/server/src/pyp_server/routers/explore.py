"""查询与查看页（03）。/api/data/{source} = Tabulator remote 后端；/data/{source} = SSR 查看页。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.crawl.ingest import build_data_table
from payipa.db.engine import get_engine
from payipa.explore.query import query_data
from sqlalchemy.exc import ProgrammingError
from starlette.datastructures import QueryParams

from pyp_server.auth import get_current_user, require_perm

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


@router.get("/data/{source}", response_class=HTMLResponse, summary="数据查看页（SSR + Tabulator remote）")
async def data_page(source: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "data.html", {"source": source, "user": user, "active": "sources"}
    )
