"""建源界面（SSR）：源列表 / 建源表单 / 提交运行。页面级登录保护（未登录跳 /login）。

建源提交实际会触发采集（与 /api/sources/{uuid}/run 同效），故 RBAC 开启时同样要过
`sources.write` 权限（M5 闸门，SSR 侧渲染友好错误而非裸 403 JSON）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.crawl.run import review_source_access
from payipa.db.engine import get_engine
from payipa.db.pyp import Source
from payipa.security.audit import record_audit_best_effort
from payipa.security.rbac import effective_permissions, has_permission
from payipa_contracts import CleanOp, FieldRule, FieldType, Locator, LocatorType, RulePack
from sqlalchemy import select

from pyp_server.auth import get_current_user
from pyp_server.service import dispatch_source_run
from pyp_server.settings import get_server_settings

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
            await conn.execute(
                select(
                    Source.uuid,
                    Source.name,
                    Source.created_at,
                    Source.access_confirmed_at,
                    Source.paused_at,
                ).order_by(Source.id.desc())
            )
        ).all()
    sources = [
        {
            "uuid": r[0],
            "name": r[1],
            "created_at": r[2].isoformat() if r[2] else "",
            "access_status": "已暂停" if r[4] else ("已确认" if r[3] else "待复核"),
            "access_class": "red" if r[4] else ("green" if r[3] else "amber"),
        }
        for r in rows
    ]
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


@router.get("/sources/{uuid}/access-review", response_class=HTMLResponse, summary="数据源访问复核")
async def source_access_review_page(uuid: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    if get_server_settings().rbac_enabled:
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, "sources.write"):
            raise HTTPException(status_code=403, detail="缺少权限：sources.write")
    async with get_engine("pyp").connect() as conn:
        row = (
            await conn.execute(
                select(
                    Source.uuid,
                    Source.name,
                    Source.access_basis,
                    Source.access_reference,
                    Source.access_confirmed_at,
                    Source.paused_at,
                    Source.pause_reason,
                ).where(Source.uuid == uuid)
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"数据源 {uuid!r} 不存在")
    source = {
        "uuid": row[0],
        "name": row[1],
        "access_basis": row[2],
        "access_reference": row[3],
        "confirmed": row[4] is not None,
        "paused": row[5] is not None,
        "pause_reason": row[6],
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "source_access_review.html",
        {"user": user, "source": source, "error": None, "active": "sources"},
    )


@router.post("/sources/{uuid}/access-review", summary="提交数据源访问复核")
async def source_access_review_submit(uuid: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    if get_server_settings().rbac_enabled:
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, "sources.write"):
            raise HTTPException(status_code=403, detail="缺少权限：sources.write")
    form = await request.form()
    access_basis = (str(form.get("access_basis") or "")).strip()
    access_reference = (str(form.get("access_reference") or "")).strip()
    approved = form.get("decision") == "approve"
    reason = (str(form.get("reason") or "")).strip() or None
    try:
        updated = await review_source_access(
            get_engine("pyp"),
            uuid,
            access_basis=access_basis,
            access_reference=access_reference,
            approved=approved,
            reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail=f"数据源 {uuid!r} 不存在")
    await record_audit_best_effort(
        get_engine("pyp"),
        action="source.access_review",
        actor_id=int(user["id"]),
        object_type="source",
        object_id=uuid,
        after={
            "access_basis": access_basis,
            "access_reference": access_reference,
            "approved": approved,
            "reason": reason,
        },
        source="web",
    )
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/create", summary="建源提交 → 运行 → 跳查看页")
async def sources_create(request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    if get_server_settings().rbac_enabled:
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, "sources.write"):
            return request.app.state.templates.TemplateResponse(
                request,
                "source_new.html",
                {"user": user, "error": "缺少权限：sources.write（创建/编辑数据源）", "active": "sources"},
                status_code=403,
            )
    form = await request.form()
    name = (str(form.get("name") or "")).strip()
    uuid = (str(form.get("uuid") or "")).strip()
    seed_urls = [u.strip() for u in str(form.get("seed_urls") or "").splitlines() if u.strip()]
    access_basis = (str(form.get("access_basis") or "")).strip()
    access_reference = (str(form.get("access_reference") or "")).strip()
    access_confirmed = form.get("access_confirmed") == "on"
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

    if not uuid or not seed_urls or not fields or not access_reference or not access_confirmed:
        return request.app.state.templates.TemplateResponse(
            request,
            "source_new.html",
            {
                "user": user,
                "error": "数据源短码、种子 URL、字段、访问依据及确认项均为必填",
                "active": "sources",
            },
            status_code=400,
        )

    rule = RulePack(
        fields=fields,
        item_locator=Locator(type=LocatorType.CSS, expr=item_expr) if item_expr else None,
        fingerprint=fingerprint,
    )
    try:
        await dispatch_source_run(
            uuid=uuid,
            name=name or uuid,
            seed_urls=seed_urls,
            rule=rule,
            indexed_fields=fingerprint,
            access_basis=access_basis,
            access_reference=access_reference,
            access_confirmed=access_confirmed,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "source_new.html",
            {"user": user, "error": str(exc), "active": "sources"},
            status_code=400,
        )
    await record_audit_best_effort(
        get_engine("pyp"),
        action="source.create",
        actor_id=int(user["id"]),
        object_type="source",
        object_id=uuid,
        after={"access_basis": access_basis, "access_reference": access_reference},
        source="web",
    )
    return RedirectResponse(f"/data/{uuid}", status_code=303)
