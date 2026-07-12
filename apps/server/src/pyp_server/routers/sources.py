"""建源界面（SSR）：源列表 / 建源表单 / 提交运行。页面级登录保护（未登录跳 /login）。

建源提交实际会触发采集（与 /api/sources/{uuid}/run 同效），故 RBAC 开启时同样要过
`sources.write` 权限（M5 闸门，SSR 侧渲染友好错误而非裸 403 JSON）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.crawl.run import batch_progress, review_source_access
from payipa.db.engine import get_engine
from payipa.db.pyp import Batch, Rule, Source, Task
from payipa.security.audit import record_audit_best_effort
from payipa.security.rbac import effective_permissions, has_permission
from payipa_contracts import CleanOp, EngineHint, FieldRule, FieldType, Locator, LocatorType, RulePack
from sqlalchemy import select

from pyp_server.auth import get_current_user
from pyp_server.csrf import render_with_csrf, verify_csrf
from pyp_server.routers.ui import page_ctx
from pyp_server.service import dispatch_source_run
from pyp_server.settings import get_server_settings

router = APIRouter(tags=["sources-ui"])

_TYPE_MAP = {"store": FieldType.STORE, "link": FieldType.LINK, "store+link": FieldType.STORE_LINK}


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@router.get("/sources", response_class=HTMLResponse, summary="数据源列表")
async def sources_list(request: Request, q: str | None = Query(None, max_length=64)):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    stmt = select(
        Source.uuid,
        Source.name,
        Source.created_at,
        Source.access_confirmed_at,
        Source.paused_at,
    ).order_by(Source.id.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Source.name.ilike(like) | Source.uuid.ilike(like))
    async with get_engine("pyp").connect() as conn:
        rows = (await conn.execute(stmt)).all()
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
        request, "sources_list.html", await page_ctx(user, sources=sources, q=q, active="sources")
    )


def _form_snapshot(form) -> dict:
    """把提交的建源表单原样收集成回显用字典（校验失败重渲染时回填，P0-25：不丢输入）。"""
    rows = [
        {"name": n, "css": c, "type": t}
        for n, c, t in zip(
            form.getlist("field_name"), form.getlist("field_css"), form.getlist("field_type"), strict=False
        )
    ]
    return {
        "name": str(form.get("name") or ""),
        "uuid": str(form.get("uuid") or ""),
        "seed_urls": str(form.get("seed_urls") or ""),
        "access_basis": str(form.get("access_basis") or ""),
        "access_reference": str(form.get("access_reference") or ""),
        "access_confirmed": form.get("access_confirmed") == "on",
        "engine_hint": str(form.get("engine_hint") or "http"),
        "rate_limit": str(form.get("rate_limit") or "10"),
        "retry": str(form.get("retry") or "3"),
        "timeout": str(form.get("timeout") or "30"),
        "raw_archive": form.get("raw_archive") == "on",
        "item_locator": str(form.get("item_locator") or ""),
        "fingerprint": str(form.get("fingerprint") or ""),
        "fields": rows or None,
    }


@router.get("/sources/new", response_class=HTMLResponse, summary="建源表单")
async def sources_new(request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    return render_with_csrf(request, "source_new.html", await page_ctx(user, error=None, form=None, active="sources"))


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
    return render_with_csrf(
        request,
        "source_access_review.html",
        await page_ctx(user, source=source, error=None, active="sources"),
    )


@router.post("/sources/{uuid}/access-review", summary="提交数据源访问复核")
async def source_access_review_submit(uuid: str, request: Request):
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))
    if get_server_settings().rbac_enabled:
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, "sources.write"):
            raise HTTPException(status_code=403, detail="缺少权限：sources.write")
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
    form = await request.form()
    snap = _form_snapshot(form)  # 一次性快照；任何错误分支都回填它（不丢用户输入）

    async def _back(error: str, status: int = 400):
        return render_with_csrf(
            request,
            "source_new.html",
            await page_ctx(user, error=error, form=snap, active="sources"),
            status_code=status,
        )

    try:
        verify_csrf(request, form.get("csrf_token"))
    except HTTPException as exc:
        return await _back(str(exc.detail), 403)

    if get_server_settings().rbac_enabled:
        perms = await effective_permissions(get_engine("pyp"), int(user["id"]))
        if not has_permission(perms, "sources.write"):
            return await _back("缺少权限：sources.write（创建/编辑数据源）", 403)
    name = (str(form.get("name") or "")).strip()
    uuid = (str(form.get("uuid") or "")).strip()
    seed_urls = [u.strip() for u in str(form.get("seed_urls") or "").splitlines() if u.strip()]
    access_basis = (str(form.get("access_basis") or "")).strip()
    access_reference = (str(form.get("access_reference") or "")).strip()
    access_confirmed = form.get("access_confirmed") == "on"
    try:
        engine_hint = EngineHint(str(form.get("engine_hint") or "http"))
        rate_limit = int(str(form.get("rate_limit") or "10"))
        retry = int(str(form.get("retry") or "3"))
        timeout = int(str(form.get("timeout") or "30"))
    except ValueError:
        return await _back("运行参数格式不正确")
    raw_archive = form.get("raw_archive") == "on"
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
        return await _back("数据源短码、种子 URL、字段、访问依据及确认项均为必填")

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
            engine_hint=engine_hint,
            rate_limit=rate_limit,
            retry=retry,
            timeout=timeout,
            raw_archive=raw_archive,
        )
    except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
        return await _back(str(exc))
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


def _fmt(dt) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


# 注意：本路由须注册在 /sources/new 之后（FastAPI 按注册顺序匹配，否则 "new" 会被当成 uuid）。
@router.get("/sources/{uuid}", response_class=HTMLResponse, summary="数据源详情")
async def source_detail(uuid: str, request: Request):
    """单源全景页（§10.3）：概览 + 操作 + 任务/最近批次 + 规则版本，一次服务端查好渲染。"""
    user = await get_current_user(request)
    if user is None:
        return _login_redirect()
    engine = get_engine("pyp")
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    Source.id,
                    Source.uuid,
                    Source.name,
                    Source.created_at,
                    Source.access_basis,
                    Source.access_reference,
                    Source.access_confirmed_at,
                    Source.paused_at,
                    Source.pause_reason,
                    Source.rate_limit,
                    Source.retry,
                    Source.timeout,
                    Source.raw_archive,
                ).where(Source.uuid == uuid)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"数据源 {uuid!r} 不存在")
        sid = row.id
        task_rows = (
            await conn.execute(
                select(Task.id, Task.trigger_type, Task.priority, Task.params, Task.created_at)
                .where(Task.source_id == sid)
                .order_by(Task.id.desc())
            )
        ).all()
        batch_rows = (
            await conn.execute(
                select(Batch.id, Batch.status, Batch.channel, Batch.started_at, Batch.finished_at)
                .join(Task, Batch.task_id == Task.id)
                .where(Task.source_id == sid)
                .order_by(Batch.id.desc())
                .limit(10)
            )
        ).all()
        rule_rows = (
            await conn.execute(
                select(Rule.version, Rule.status, Rule.content_hash, Rule.created_at)
                .where(Rule.source_id == sid)
                .order_by(Rule.version.desc())
            )
        ).all()
    source = {
        "uuid": row.uuid,
        "name": row.name,
        "created_at": _fmt(row.created_at),
        "access_status": "已暂停" if row.paused_at else ("已确认" if row.access_confirmed_at else "待复核"),
        "access_class": "red" if row.paused_at else ("green" if row.access_confirmed_at else "amber"),
        "pause_reason": row.pause_reason,
        "access_basis": row.access_basis,
        "access_reference": row.access_reference,
        # 引擎存于 task.params（建源时写入）；取最新任务的，缺省 http
        "engine": (task_rows[0].params or {}).get("engine_hint", "http") if task_rows else "http",
        "rate_limit": row.rate_limit,
        "retry": row.retry,
        "timeout": row.timeout,
        "raw_archive": row.raw_archive,
    }
    tasks = [
        {
            "id": t.id,
            "trigger": t.trigger_type,
            "priority": t.priority,
            "engine": (t.params or {}).get("engine_hint", "http"),
            "created_at": _fmt(t.created_at),
        }
        for t in task_rows
    ]
    batches = []
    for b in batch_rows:  # 逐个聚合进度（≤10 次查询，复用 monitor 同款口径）
        prog = await batch_progress(engine, b.id)
        batches.append(
            {
                "id": b.id,
                "status": b.status,
                "channel": b.channel,
                "started_at": _fmt(b.started_at),
                "finished_at": _fmt(b.finished_at),
                **prog,
            }
        )
    rules = [
        {
            "version": r.version,
            "status": r.status,
            "hash": (r.content_hash or "")[:12],
            "created_at": _fmt(r.created_at),
        }
        for r in rule_rows
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "source_detail.html",
        await page_ctx(user, source=source, tasks=tasks, batches=batches, rules=rules, active="sources"),
    )
