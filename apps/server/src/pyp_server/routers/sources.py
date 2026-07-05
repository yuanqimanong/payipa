"""建源界面（SSR）：源列表 / 建源表单 / 提交运行。页面级登录保护（未登录跳 /login）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.db.engine import get_engine
from payipa.db.pyp import Source
from payipa_contracts import CleanOp, FieldRule, FieldType, Locator, LocatorType, RulePack
from sqlalchemy import select

from pyp_server.auth import get_current_user
from pyp_server.service import dispatch_source_run

router = APIRouter(tags=["sources-ui"])

_TYPE_MAP = {"store": FieldType.STORE, "link": FieldType.LINK, "store+link": FieldType.STORE_LINK}


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@router.get("/sources", response_class=HTMLResponse, summary="数据源列表")
async def sources_list(request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    async with get_engine("pyp").connect() as conn:
        rows = (
            await conn.execute(select(Source.uuid, Source.name, Source.created_at).order_by(Source.id.desc()))
        ).all()
    sources = [{"uuid": r[0], "name": r[1], "created_at": r[2].isoformat() if r[2] else ""} for r in rows]
    return request.app.state.templates.TemplateResponse(
        request, "sources_list.html", {"user": user, "sources": sources, "active": "sources"}
    )


@router.get("/sources/new", response_class=HTMLResponse, summary="建源表单")
async def sources_new(request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    return request.app.state.templates.TemplateResponse(
        request, "source_new.html", {"user": user, "error": None, "active": "sources"}
    )


@router.post("/sources/create", summary="建源提交 → 运行 → 跳查看页")
async def sources_create(request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    form = await request.form()
    name = (str(form.get("name") or "")).strip()
    uuid = (str(form.get("uuid") or "")).strip()
    seed_urls = [u.strip() for u in str(form.get("seed_urls") or "").splitlines() if u.strip()]
    item_expr = (str(form.get("item_locator") or "")).strip()
    fingerprint = [x.strip() for x in str(form.get("fingerprint") or "").split(",") if x.strip()]

    names = form.getlist("field_name")
    csss = form.getlist("field_css")
    types = form.getlist("field_type")
    fields: list[FieldRule] = []
    for n, css, t in zip(names, csss, types, strict=False):
        n, css = str(n).strip(), str(css).strip()
        if not n or not css:
            continue
        ftype = _TYPE_MAP.get(str(t).strip(), FieldType.STORE)
        clean = [CleanOp(op="url_normalize")] if ftype in (FieldType.LINK, FieldType.STORE_LINK) else []
        fields.append(
            FieldRule(
                name=n,
                locator=Locator(type=LocatorType.CSS, expr=css),
                type=ftype,
                clean=clean,
                index=n in fingerprint,
            )
        )

    if not uuid or not seed_urls or not fields:
        return request.app.state.templates.TemplateResponse(
            request,
            "source_new.html",
            {"user": user, "error": "数据源短码、种子 URL、至少一个字段 为必填", "active": "sources"},
            status_code=400,
        )

    rule = RulePack(
        fields=fields,
        item_locator=Locator(type=LocatorType.CSS, expr=item_expr) if item_expr else None,
        fingerprint=fingerprint,
    )
    await dispatch_source_run(
        request.app.state.hub,
        uuid=uuid,
        name=name or uuid,
        seed_urls=seed_urls,
        rule=rule,
        indexed_fields=fingerprint,
    )
    return RedirectResponse(f"/data/{uuid}", status_code=303)
