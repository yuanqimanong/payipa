"""结果提交与 fencing：先写 data_center 幂等数据，再更新 pyp 状态。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from payipa_contracts import (
    Channel,
    RequestState,
    ResultBatch,
    RulePack,
)
from sqlalchemy import Table, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl._policy import _is_stale
from payipa.crawl.frontier import _enqueue_discovered_conn
from payipa.crawl.ingest import Ingestor
from payipa.db.pyp import Batch, Request, Rule, Source, Task, TaskEvent


async def resolve_ingest_context(engine_pyp: AsyncEngine, req_id: int) -> tuple[str, list[str], list[str], Channel]:
    """由 req_id 反解入库上下文：(source_uuid, fingerprint_keys, indexed_fields, channel)。"""
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Source.uuid, Rule.spec, Batch.channel)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .join(Rule.__table__, Rule.id == Request.rule_id)
                .where(Request.id == req_id)
            )
        ).first()
    if row is None:
        raise LookupError(f"无法反解 req_id={req_id} 的入库上下文")
    uuid, spec, channel = row
    pack = RulePack.model_validate(spec)
    indexed = [f.name for f in pack.fields if f.index]
    return uuid, list(pack.fingerprint), indexed, Channel(channel)


async def fence_ok(engine_pyp: AsyncEngine, req_id: int, agent_id: str | None, attempt: int | None) -> bool:
    """只读 fencing 预检（供 ws 在续爬入队前调用）；stale 时记 task_event 并返回 False。

    权威校验仍在 :func:`handle_result` 事务内再做一次（预检到写入之间的窄窗口竞态由其兜底）。
    """
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Request.state, Request.agent_id, Request.attempt, Request.batch_id).where(Request.id == req_id)
            )
        ).first()
        if not _is_stale(row, agent_id, attempt):
            return True
        if row is not None:
            await conn.execute(
                TaskEvent.__table__.insert().values(
                    batch_id=row.batch_id,
                    type="request.stale_result",
                    payload={"req_id": req_id, "agent_id": agent_id, "attempt": attempt, "state": int(row.state)},
                )
            )
    return False


@dataclass(frozen=True, slots=True)
class ResultCommit:
    accepted: bool
    written: int = 0
    discovered: int = 0


async def commit_result(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    table: Table,
    result: ResultBatch,
    *,
    fingerprint_keys: Sequence[str] = (),
    agent_id: str | None = None,
) -> ResultCommit:
    """原子提交结果的控制面副作用，并返回是否通过 fencing。

    fencing（P0-10）：在 pyp 事务内先锁行校验（状态在途 + agent 归属 + attempt 代次），迟到/重派后的
    旧结果既不写数据、不派生链接，也不改状态。锁行在 data_center 写之前取得，保持「状态=成功 ⟹ 数据已落」。
    同时把 agent 回报的执行摘要计数落到 request 行，供 core.monitor 聚合数据质量与时延（M5）。
    """
    s = result.summary
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(Request.state, Request.agent_id, Request.attempt, Request.batch_id)
                .where(Request.id == int(result.req_id))
                .with_for_update()
            )
        ).first()
        if _is_stale(row, agent_id, int(result.attempt)):
            if row is not None:
                await conn.execute(
                    TaskEvent.__table__.insert().values(
                        batch_id=row.batch_id,
                        type="request.stale_result",
                        payload={
                            "req_id": int(result.req_id),
                            "agent_id": agent_id,
                            "attempt": int(result.attempt),
                            "state": int(row.state),
                        },
                    )
                )
            return ResultCommit(accepted=False)
        discovered = await _enqueue_discovered_conn(conn, int(result.req_id), result.discovered)
        # 持有 pyp 行锁跨库写数据：先数据后状态的顺序不变（SDD §4.4）
        written = await Ingestor(engine_dc).upsert(
            table, result.items, batch_id=int(result.batch_id), fingerprint_keys=fingerprint_keys
        )
        source_id = (
            await conn.execute(
                select(Task.source_id)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .where(Request.id == int(result.req_id))
            )
        ).scalar()
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == int(result.req_id))
            .values(
                state=int(RequestState.SUCCESS),
                lease_until=None,  # 完成即释放租约，免遭 reaper 回收
                not_before=None,
                error_code=None,
                reason_code=None,
                error_detail=None,
                retry_after_s=None,
                response_status=s.response_status,
                count_ok=int(s.count_ok),
                count_fail=int(s.count_fail),
                count_blank=int(s.count_blank),
                duration_ms=int(s.elapsed_s * 1000),
            )
        )
        if source_id is not None:
            await conn.execute(
                update(Source.__table__)
                .where(Source.id == source_id)
                .values(
                    last_status_code=s.response_status,
                    last_success_at=func.now(),
                    consecutive_failures=0,
                    cooldown_until=case((Source.cooldown_until <= func.now(), None), else_=Source.cooldown_until),
                    cooldown_reason=case((Source.cooldown_until <= func.now(), None), else_=Source.cooldown_reason),
                )
            )
    return ResultCommit(accepted=True, written=written, discovered=discovered)


async def handle_result(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    table: Table,
    result: ResultBatch,
    *,
    fingerprint_keys: Sequence[str] = (),
    agent_id: str | None = None,
) -> int:
    """兼容调用：提交结果并返回写入行数；需要区分 stale 时请调用 :func:`commit_result`。"""
    committed = await commit_result(
        engine_pyp,
        engine_dc,
        table,
        result,
        fingerprint_keys=fingerprint_keys,
        agent_id=agent_id,
    )
    return committed.written


async def set_request_state(
    engine_pyp: AsyncEngine,
    req_id: int,
    state: int,
    *,
    agent_id: str | None = None,
    attempt: int | None = None,
    reason_code: str | None = None,
    message: str | None = None,
) -> int:
    """置请求状态（正=正常态、负=错误码）。失败/取消回报走此。终态一律释放租约。

    fencing（P0-10）：只有未终结（QUEUED/ASSIGNED/RUNNING）的请求可被置态——成功/取消/失败后的
    迟到回报一律不覆盖；传 agent_id/attempt 时归属与代次也须吻合。返回受影响行数。
    """
    conds = [
        Request.id == req_id,
        Request.state.in_((int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING))),
    ]
    if agent_id is not None:
        conds.append(Request.agent_id == agent_id)
    if attempt is not None:
        conds.append(Request.attempt == attempt)
    values: dict = {
        "state": state,
        "error_code": state if state < 0 else None,
        "lease_until": None,
        "not_before": None,
    }
    if reason_code is not None:
        values["reason_code"] = reason_code[:64]
    if message is not None:
        values["error_detail"] = message[:1000]
    async with engine_pyp.begin() as conn:
        res = await conn.execute(update(Request.__table__).where(*conds).values(**values))
    return res.rowcount
