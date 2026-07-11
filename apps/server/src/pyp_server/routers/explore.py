"""Structured data query API and server-rendered data page."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from payipa.crawl.ingest import build_data_table
from payipa.crawl.run import source_field_names
from payipa.db.engine import get_engine
from payipa.explore.export import stream_csv, stream_jsonl
from payipa.explore.query import query_data
from sqlalchemy.exc import ProgrammingError
from starlette.datastructures import QueryParams

from pyp_server.auth import get_current_user, require_perm

router = APIRouter(tags=["explore"])

_BRACKET = re.compile(r"(\w+)\[(\d+)\]\[(\w+)\]")


def _collect(qp: QueryParams, prefix: str) -> list[dict]:
    """Parse Tabulator's indexed sort/filter query parameters."""
    buckets: dict[int, dict] = {}
    for key, value in qp.multi_items():
        match = _BRACKET.match(key)
        if match and match.group(1) == prefix:
            buckets.setdefault(int(match.group(2)), {})[match.group(3)] = value
    return [buckets[index] for index in sorted(buckets)]


def _parse_tabulator(qp: QueryParams) -> tuple[int, int, list[dict], list[dict]]:
    page = int(qp.get("page") or 1)
    size = int(qp.get("size") or 50)
    return page, size, _collect(qp, "sort"), _collect(qp, "filter")


@router.get(
    "/api/data/{source}",
    summary="查询数据源产出（结构化筛选、排序和分页）",
    dependencies=[Depends(require_perm("data.read"))],
)
async def get_data(source: str, request: Request) -> dict:
    page, size, sorters, filters = _parse_tabulator(request.query_params)
    table = build_data_table(source)
    try:
        return await query_data(
            get_engine("data_center"),
            table,
            page=page,
            size=size,
            sorters=sorters,
            filters=filters,
        )
    except ProgrammingError:
        return {"last_page": 1, "data": [], "total": 0}


def _safe_filename(source: str, ext: str) -> str:
    """下载文件名：短码只保留安全字符，避免响应头注入。"""
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", source)[:64] or "data"
    return f"{stem}.{ext}"


@router.get(
    "/api/data/{source}/export",
    summary="导出数据源产出（CSV / JSONL 流式下载）",
    dependencies=[Depends(require_perm("data.read"))],
)
async def export_data(source: str, request: Request, fmt: str = "csv") -> StreamingResponse:
    """流式导出全部产出行。fmt=csv（列=系统列+规则字段，带 BOM）| jsonl（每行一 JSON）。"""
    if fmt not in ("csv", "jsonl"):
        raise HTTPException(status_code=400, detail="fmt 仅支持 csv 或 jsonl")
    filters = _collect(request.query_params, "filter")
    dc = get_engine("data_center")
    table = build_data_table(source)
    if fmt == "jsonl":
        gen = stream_jsonl(dc, table, filters=filters)
        media, ext = "application/x-ndjson", "jsonl"
    else:
        field_names = await source_field_names(get_engine("pyp"), source)
        gen = stream_csv(dc, table, field_names=field_names, filters=filters)
        media, ext = "text/csv; charset=utf-8", "csv"
    headers = {"Content-Disposition": f'attachment; filename="{_safe_filename(source, ext)}"'}
    return StreamingResponse(gen, media_type=media, headers=headers)


@router.get("/data/{source}", response_class=HTMLResponse, summary="数据查看页")
async def data_page(source: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "data.html",
        {"source": source, "user": user, "active": "sources"},
    )
