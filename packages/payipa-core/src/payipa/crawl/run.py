"""批次/请求创建 + 结果分流入库（M1 单源端到端最小实现）。

跨库写入一致性（无分布式事务）：**先写数据（data_center，指纹幂等）→ 再置状态（pyp 的 requests/batch）**，
顺序保证「状态=成功 ⟹ 数据已落」。规模内不引入分布式事务/重型对账（SDD §4.4）。
"""

from __future__ import annotations

from collections.abc import Sequence

from payipa_contracts import Channel, RequestState, ResultBatch, RulePack, RulePointer, TaskSpec
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

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
            update(Request.__table__).where(Request.id == int(result.req_id)).values(state=int(RequestState.SUCCESS))
        )
    return written


async def set_request_state(engine_pyp: AsyncEngine, req_id: int, state: int) -> None:
    """置请求状态（正=正常态、负=错误码）。失败/取消回报走此。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(state=state, error_code=state if state < 0 else None)
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
