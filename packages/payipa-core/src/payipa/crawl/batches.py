"""批次生命周期：创建、进度、收尾、取消与按存档配置重跑。"""

from __future__ import annotations

from collections.abc import Sequence

from payipa_contracts import (
    Channel,
    RequestState,
    RulePointer,
    TaskSpec,
)
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl._policy import _INFLIGHT, _allowed_domains, url_fingerprint
from payipa.db.pyp import Batch, Request, Rule, Source, Task


async def create_batch_with_requests(
    engine_pyp: AsyncEngine,
    *,
    task_id: int,
    source_uuid: str,
    targets: Sequence[str],
    rule_ptr: RulePointer,
    channel: Channel = Channel.PROD,
) -> tuple[int, list[TaskSpec]]:
    """为已批准且未暂停的数据源建批次；返回 ``(batch_id, TaskSpec 列表)``。"""
    channel = Channel(channel)
    specs: list[TaskSpec] = []
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(
                    Source.id,
                    Source.access_confirmed_at,
                    Source.paused_at,
                    Source.provisioning_state,
                    Source.raw_archive,
                )
                .select_from(Task.__table__)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Task.id == task_id, Source.uuid == source_uuid)
            )
        ).first()
        if source is None:
            raise LookupError(f"task {task_id} 与数据源 {source_uuid!r} 不匹配")
        if source.access_confirmed_at is None:
            raise PermissionError("数据源尚未完成人工访问授权复核")
        if source.paused_at is not None:
            raise PermissionError("数据源已暂停，不能创建新批次")
        if source.provisioning_state != "ready":
            raise RuntimeError(f"数据源动态表尚未就绪（{source.provisioning_state}）")
        rule_row = (
            await conn.execute(
                select(Rule.source_id, Rule.version, Rule.content_hash, Rule.status).where(
                    Rule.id == int(rule_ptr.rule_id)
                )
            )
        ).first()
        if rule_row is None:
            raise LookupError(f"规则 {rule_ptr.rule_id} 不存在")
        if int(rule_row.source_id) != int(source.id):
            raise PermissionError("规则不属于当前数据源")
        if int(rule_row.version) != rule_ptr.version or rule_row.content_hash != rule_ptr.content_hash:
            raise ValueError("规则指针的版本或内容哈希与权威记录不一致")
        if channel is Channel.PROD and rule_row.status != "active":
            raise PermissionError("生产批次只能引用 active 规则；draft/testing 仅可用于 test 通道")
        batch_id = (
            await conn.execute(
                pg_insert(Batch.__table__)
                .values(task_id=task_id, channel=channel.value, status="running", started_at=func.now(), stats={})
                .returning(Batch.id)
            )
        ).scalar_one()
        for target in targets:
            req_id = (
                await conn.execute(
                    pg_insert(Request.__table__)
                    .values(
                        batch_id=batch_id,
                        target=target,
                        rule_id=int(rule_ptr.rule_id),
                        rule_hash=rule_ptr.content_hash,
                        rule_version=rule_ptr.version,
                        state=int(RequestState.QUEUED),
                        depth=0,
                        url_hash=url_fingerprint(target),
                    )
                    .returning(Request.id)
                )
            ).scalar_one()
            specs.append(
                TaskSpec(
                    task_id=str(task_id),
                    req_id=str(req_id),
                    batch_id=str(batch_id),
                    source=source_uuid,
                    target=target,
                    allowed_domains=_allowed_domains({"seed_urls": list(targets)}, target),
                    rule_ptr=rule_ptr,
                    channel=channel,
                    archive_raw=bool(source.raw_archive) and channel is Channel.PROD,
                )
            )
    return batch_id, specs


async def finalize_batch_if_done(engine_pyp: AsyncEngine, batch_id: int) -> bool:
    """无未完成 request（state 仍为排队/分派/运行）时把 running 批次标 done。返回是否本次完成收尾。

    仅对 status='running' 的批次生效——已取消/已收尾的批次不被翻回 done。
    """
    pending_states = (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))
    async with engine_pyp.begin() as conn:
        pending = (
            await conn.execute(
                select(func.count())
                .select_from(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state.in_(pending_states))
            )
        ).scalar()
        if pending:
            return False
        res = await conn.execute(
            update(Batch.__table__)
            .where(Batch.id == batch_id, Batch.status == "running")
            .values(status="done", finished_at=func.now())
        )
    return bool(res.rowcount)


async def finalize_request_batch(engine_pyp: AsyncEngine, req_id: int) -> int | None:
    """尝试收尾请求所属批次；仅在本次完成 ``running -> done`` 时返回批次 id。"""
    async with engine_pyp.connect() as conn:
        batch_id = (await conn.execute(select(Request.batch_id).where(Request.id == req_id))).scalar()
    if batch_id is None:
        return None
    return int(batch_id) if await finalize_batch_if_done(engine_pyp, int(batch_id)) else None


async def batch_trigger_context(engine_pyp: AsyncEngine, batch_id: int) -> dict | None:
    """批次收尾自动触发所需上下文：所属任务的 params（含通知/推送绑定）+ 批次状态 + 成功计数。

    返回 ``{task_id, status, channel, params, ok, total}``；批次不存在返回 None。params 里约定键（可选）：
    ``notify_bot_id``（收尾通知机器人）/ ``push_component_id`` + ``product_code``（链路自动推送）。
    """
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Batch.task_id, Batch.status, Batch.channel, Task.params)
                .join(Task, Task.id == Batch.task_id)
                .where(Batch.id == batch_id)
            )
        ).first()
        if row is None:
            return None
        total = (
            await conn.execute(select(func.count()).select_from(Request.__table__).where(Request.batch_id == batch_id))
        ).scalar() or 0
        ok = (
            await conn.execute(
                select(func.count())
                .select_from(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state == int(RequestState.SUCCESS))
            )
        ).scalar() or 0
    return {
        "task_id": int(row.task_id),
        "status": row.status,
        "channel": row.channel,
        "params": row.params or {},
        "ok": int(ok),
        "total": int(total),
    }


async def batch_progress(engine_pyp: AsyncEngine, batch_id: int) -> dict:
    """按 state 实时聚合批次进度：{total, ok, fail, running, pct}。

    ok=SUCCESS(3)；fail=state<0；running=未终结(QUEUED/ASSIGNED/RUNNING)；pct=已终结/总数×100。
    CANCELED(4) 计入 total 且算作已终结（不入 ok/fail/running）。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Request.state, func.count()).where(Request.batch_id == batch_id).group_by(Request.state)
            )
        ).all()
    total = ok = fail = running = 0
    for state, n in rows:
        total += n
        if state == int(RequestState.SUCCESS):
            ok += n
        elif state < 0:
            fail += n
        elif state in _INFLIGHT or state == int(RequestState.QUEUED):
            running += n
    pct = round((total - running) / total * 100, 1) if total else 0.0
    return {"total": total, "ok": ok, "fail": fail, "running": running, "pct": pct}


async def cancel_batch(engine_pyp: AsyncEngine, batch_id: int) -> tuple[list[str], list[int]]:
    """取消一个批次：清空其 QUEUED（直接置 CANCELED），把在途 ASSIGNED/RUNNING 标记待取消（返回其 req_id
    供主控向 agent 发 Cancel 帧），批次置 canceling。返回 (在途 req_id 列表, 涉及的 agent 无关)。

    返回 (inflight_req_ids, queued_ids)：inflight 需通知 agent 优雅收尾，queued 已就地取消。
    """
    async with engine_pyp.begin() as conn:
        queued = list(
            (
                await conn.execute(
                    select(Request.id).where(Request.batch_id == batch_id, Request.state == int(RequestState.QUEUED))
                )
            )
            .scalars()
            .all()
        )
        if queued:
            await conn.execute(
                update(Request.__table__)
                .where(Request.batch_id == batch_id, Request.state == int(RequestState.QUEUED))
                .values(state=int(RequestState.CANCELED), lease_until=None)
            )
        inflight = list(
            (await conn.execute(select(Request.id).where(Request.batch_id == batch_id, Request.state.in_(_INFLIGHT))))
            .scalars()
            .all()
        )
        await conn.execute(
            update(Batch.__table__)
            .where(Batch.id == batch_id, Batch.status == "running")
            .values(status="canceling" if inflight else "canceled", finished_at=None if inflight else func.now())
        )
    return [str(r) for r in inflight], queued


async def sweep_canceling_batches(engine_pyp: AsyncEngine) -> int:
    """把已无未完成请求的 canceling 批次收口为 canceled（在途 agent 回报 CANCELED 后）。返回收口数。"""
    pending = (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))
    async with engine_pyp.begin() as conn:
        stuck = select(Request.batch_id).where(Request.state.in_(pending)).distinct().scalar_subquery()
        res = await conn.execute(
            update(Batch.__table__)
            .where(Batch.status == "canceling", Batch.id.not_in(stuck))
            .values(status="canceled", finished_at=func.now())
        )
    return res.rowcount or 0


async def create_batch_for_task(
    engine_pyp: AsyncEngine,
    *,
    task_id: int,
    source_uuid: str,
    seed_urls: Sequence[str],
    channel: Channel = Channel.PROD,
) -> tuple[int, list[TaskSpec]]:
    """按已存在的 task + 其数据源当前 active 规则建一个新批次（供 cron/API 重跑，无需重新提交规则）。"""
    async with engine_pyp.connect() as conn:
        query = (
            select(Rule.id, Rule.version, Rule.content_hash)
            .join(Task.__table__, Task.source_id == Rule.source_id)
            .where(Task.id == task_id)
        )
        if Channel(channel) is Channel.PROD:
            query = query.where(Rule.status == "active")
        row = (await conn.execute(query.order_by(Rule.version.desc()).limit(1))).first()
    if row is None:
        raise LookupError(f"task {task_id} 无可用规则，无法建批次")
    ptr = RulePointer(rule_id=str(row[0]), version=row[1], content_hash=row[2])
    return await create_batch_with_requests(
        engine_pyp, task_id=task_id, source_uuid=source_uuid, targets=seed_urls, rule_ptr=ptr, channel=channel
    )


async def rerun_source(engine_pyp: AsyncEngine, source_uuid: str, *, channel: Channel = Channel.PROD) -> int:
    """按数据源存档配置（task.params 里的种子 + 当前 active 规则）重跑一次，返回新批次 id。

    无需重新提交种子/规则（07 定时触发同源）。访问策略闸门在 create_batch_with_requests 内生效
    （未确认/暂停源抛 PermissionError）。缺任务或缺种子抛 LookupError。
    """
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(Task.id, Task.params)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Source.uuid == source_uuid)
                .order_by(Task.id)
                .limit(1)
            )
        ).first()
    if row is None:
        raise LookupError(f"数据源 {source_uuid!r} 无关联任务，无法重跑")
    seeds = list((row[1] or {}).get("seed_urls") or [])
    if not seeds:
        raise LookupError(f"数据源 {source_uuid!r} 未存档种子 URL（task.params.seed_urls 为空），无法重跑")
    batch_id, _ = await create_batch_for_task(
        engine_pyp, task_id=row[0], source_uuid=source_uuid, seed_urls=seeds, channel=channel
    )
    return batch_id
