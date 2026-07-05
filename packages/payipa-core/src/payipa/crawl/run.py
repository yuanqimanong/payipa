"""批次/请求创建 + 结果分流入库（M1 单源端到端最小实现）。

跨库写入一致性（无分布式事务）：**先写数据（data_center，指纹幂等）→ 再置状态（pyp 的 requests/batch）**，
顺序保证「状态=成功 ⟹ 数据已落」。规模内不引入分布式事务/重型对账（SDD §4.4）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from payipa_contracts import Channel, ErrorCode, RequestState, ResultBatch, RulePack, RulePointer, TaskSpec
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from payipa.crawl.ingest import Ingestor, build_data_table, create_data_table
from payipa.db.pyp import Batch, Request, Rule, Source, Task


async def setup_source(engine_pyp: AsyncEngine, uuid: str, name: str = "M1 source") -> tuple[int, int]:
    """确保 source + 一个 task 存在（幂等）；返回 (source_id, task_id)。M1 便捷入口。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(Source.__table__)
            .values(uuid=uuid, name=name, connector_type="web")
            .on_conflict_do_nothing(index_elements=["uuid"])
        )
        source_id = (await conn.execute(select(Source.id).where(Source.uuid == uuid))).scalar_one()
        task_id = (await conn.execute(select(Task.id).where(Task.source_id == source_id).limit(1))).scalar()
        if task_id is None:
            task_id = (
                await conn.execute(
                    pg_insert(Task.__table__).values(source_id=source_id, trigger_type="manual").returning(Task.id)
                )
            ).scalar_one()
    return source_id, task_id


async def resolve_ingest_context(engine_pyp: AsyncEngine, req_id: int) -> tuple[str, list[str], list[str]]:
    """由 req_id 反解入库上下文：(source_uuid, fingerprint_keys, indexed_fields)。"""
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Source.uuid, Rule.spec)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .join(Rule.__table__, Rule.content_hash == Request.rule_hash)
                .where(Request.id == req_id)
            )
        ).first()
    if row is None:
        raise LookupError(f"无法反解 req_id={req_id} 的入库上下文")
    uuid, spec = row
    pack = RulePack.model_validate(spec)
    indexed = [f.name for f in pack.fields if f.index]
    return uuid, list(pack.fingerprint), indexed


async def ensure_data_table(engine_dc: AsyncEngine, source_uuid: str, indexed_fields: Sequence[str] = ()) -> Table:
    """建源时程序化建 data_{uuid} 表（幂等）。"""
    table = build_data_table(source_uuid, indexed_fields)
    await create_data_table(engine_dc, table)
    return table


async def create_batch_with_requests(
    engine_pyp: AsyncEngine,
    *,
    task_id: int,
    source_uuid: str,
    targets: Sequence[str],
    rule_ptr: RulePointer,
    channel: Channel = Channel.PROD,
) -> tuple[int, list[TaskSpec]]:
    """建一个批次 + 每个 target 一条 request；返回 (batch_id, [TaskSpec])。"""
    specs: list[TaskSpec] = []
    async with engine_pyp.begin() as conn:
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
                        rule_hash=rule_ptr.content_hash,
                        rule_version=rule_ptr.version,
                        state=int(RequestState.QUEUED),
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
                    rule_ptr=rule_ptr,
                    channel=channel,
                )
            )
    return batch_id, specs


async def handle_result(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    table: Table,
    result: ResultBatch,
    *,
    fingerprint_keys: Sequence[str] = (),
) -> int:
    """收到 ResultBatch：先入库 data_center（指纹幂等）→ 再置 request 成功。返回入库行数。"""
    written = await Ingestor(engine_dc).upsert(
        table, result.items, batch_id=int(result.batch_id), fingerprint_keys=fingerprint_keys
    )
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == int(result.req_id))
            .values(state=int(RequestState.SUCCESS), lease_until=None)  # 完成即释放租约，免遭 reaper 回收
        )
    return written


async def set_request_state(engine_pyp: AsyncEngine, req_id: int, state: int) -> None:
    """置请求状态（正=正常态、负=错误码）。失败/取消回报走此。终态一律释放租约。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(state=state, error_code=state if state < 0 else None, lease_until=None)
        )


async def finalize_batch_if_done(engine_pyp: AsyncEngine, batch_id: int) -> bool:
    """无未完成 request（state 仍为排队/分派/运行）时把批次标 done。返回是否已收尾。"""
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
        await conn.execute(
            update(Batch.__table__).where(Batch.id == batch_id).values(status="done", finished_at=func.now())
        )
    return True


# ── M2 派发/回收（PG 权威；由 server 的后台派发环调用，core 不启循环）─────────────
_INFLIGHT = (int(RequestState.ASSIGNED), int(RequestState.RUNNING))  # 「在途」= 已占用未终结


async def claim_queued_for_dispatch(engine_pyp: AsyncEngine, *, limit: int = 16) -> list[TaskSpec]:
    """只读扫描 running 批次下 state=QUEUED 的请求（FIFO by created_at），组装成可下发的 TaskSpec。

    **不改状态**——真正占用由 :func:`mark_assigned` 的乐观锁完成，避免读到即算派发。
    优先级/深度排序留后续 M2 切片（当前纯 FIFO）。
    """
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    Request.id,
                    Request.target,
                    Request.rule_hash,
                    Request.rule_version,
                    Batch.id,
                    Batch.channel,
                    Task.id,
                    Source.uuid,
                    Rule.id,
                )
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .join(Rule.__table__, Rule.content_hash == Request.rule_hash)
                .where(Request.state == int(RequestState.QUEUED), Batch.status == "running")
                .order_by(Request.created_at, Request.id)
                .limit(limit)
            )
        ).all()
    specs: list[TaskSpec] = []
    for req_id, target, rule_hash, rule_version, batch_id, channel, task_id, source_uuid, rule_id in rows:
        specs.append(
            TaskSpec(
                task_id=str(task_id),
                req_id=str(req_id),
                batch_id=str(batch_id),
                source=source_uuid,
                target=target,
                rule_ptr=RulePointer(rule_id=str(rule_id), version=int(rule_version or 0), content_hash=rule_hash),
                channel=Channel(channel),
            )
        )
    return specs


async def mark_assigned(engine_pyp: AsyncEngine, req_id: int, agent_id: str, lease_until: datetime) -> int:
    """乐观占用：仅当仍为 QUEUED 才置 ASSIGNED 并写 agent_id/lease_until。返回受影响行数（1=占用成功）。

    调用方必须先检查返回 1 再下发 TaskAssign，否则可能重复派发同一请求。
    """
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id, Request.state == int(RequestState.QUEUED))
            .values(state=int(RequestState.ASSIGNED), agent_id=agent_id, lease_until=lease_until)
        )
    return res.rowcount


async def requeue_request(engine_pyp: AsyncEngine, req_id: int) -> int:
    """把一条已 ASSIGNED 但下发失败（WS 发送异常）的请求退回 QUEUED；未真正执行，不计 attempt。"""
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id, Request.state == int(RequestState.ASSIGNED))
            .values(state=int(RequestState.QUEUED), lease_until=None, agent_id=None)
        )
    return res.rowcount


async def _requeue_or_giveup(conn: AsyncConnection, base_where: list, max_attempt: int) -> int:
    """符合 base_where 的在途请求：未达 max_attempt → 回 QUEUED(attempt+1)；已达 → 定格 NODE_LOST(-6)。"""
    give_up = await conn.execute(
        update(Request.__table__)
        .where(*base_where, Request.state.in_(_INFLIGHT), Request.attempt + 1 >= max_attempt)
        .values(state=int(ErrorCode.NODE_LOST), error_code=int(ErrorCode.NODE_LOST), lease_until=None)
    )
    requeue = await conn.execute(
        update(Request.__table__)
        .where(*base_where, Request.state.in_(_INFLIGHT), Request.attempt + 1 < max_attempt)
        .values(state=int(RequestState.QUEUED), attempt=Request.attempt + 1, lease_until=None, agent_id=None)
    )
    return (give_up.rowcount or 0) + (requeue.rowcount or 0)


async def requeue_expired_leases(engine_pyp: AsyncEngine, *, max_attempt: int = 3) -> int:
    """回收租约到期（agent 疑似失联/挂起）的在途请求。以 DB 时钟 func.now() 为准，避免应用/库时钟偏差。"""
    async with engine_pyp.begin() as conn:
        return await _requeue_or_giveup(
            conn, [Request.lease_until.is_not(None), Request.lease_until < func.now()], max_attempt
        )


async def requeue_agent_inflight(engine_pyp: AsyncEngine, agent_id: str, *, max_attempt: int = 3) -> int:
    """agent 断连即回收其在途请求（快速路径，不等租约超时）；由 WS 端点在 finally 中调用。"""
    async with engine_pyp.begin() as conn:
        return await _requeue_or_giveup(conn, [Request.agent_id == agent_id], max_attempt)


# ── M2 监控聚合（供 /api/monitor 端点；仅读）──────────────────────────────────
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


async def queue_depth(engine_pyp: AsyncEngine) -> dict[str, int]:
    """running 批次下 state=QUEUED 请求的排队深度。M2 首刀仅单桶（优先级排序未接线，诚实归 'mid'）。"""
    async with engine_pyp.connect() as conn:
        n = (
            await conn.execute(
                select(func.count())
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .where(Request.state == int(RequestState.QUEUED), Batch.status == "running")
            )
        ).scalar()
    return {"mid": int(n or 0)}
