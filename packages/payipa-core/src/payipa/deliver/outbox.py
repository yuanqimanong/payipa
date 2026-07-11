"""推送 outbox 状态机 + 主控 Consumer 排空（M4 slice-1）。

pending →(claim+lease)→ inflight →(deliver ok)→ sent | (fail, attempts++ 退避)→ pending... → dead(达上限)。
idempotency_key 幂等去重（partial-unique 索引）。inflight 租约到期回收（Consumer 崩溃恢复）。退避复用
jianbing_utils.retry.backoff_delay。投递通道由注入的 Deliverer 负责（主控隔离子进程执行器在后续切片）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from jianbing_utils.retry import backoff_delay
from payipa_contracts import OutboxState
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import PushOutbox

# 投递器：拿一条 outbox 记录（dict），投递成功即返回，失败抛异常（触发退避/死信）。
Deliverer = Callable[[dict], Awaitable[None]]


async def enqueue_push(
    engine_pyp: AsyncEngine,
    *,
    component_id: int,
    payload_ref: str | None = None,
    idempotency_key: str | None = None,
    batch_id: int | None = None,
) -> int:
    """入队一条待推送（state=pending）。带 idempotency_key 时同键只入一次（ON CONFLICT DO NOTHING）。

    返回新入队行数（0 = 幂等去重命中）。业务侧应「先写数据 → 同事务置状态 + 调用本函数」以保证不丢。
    """
    async with engine_pyp.begin() as conn:
        stmt = pg_insert(PushOutbox.__table__).values(
            component_id=component_id,
            payload_ref=payload_ref,
            idempotency_key=idempotency_key,
            batch_id=batch_id,
            state=OutboxState.PENDING.value,
            attempts=0,
        )
        if idempotency_key is not None:
            stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])
        res = await conn.execute(stmt)
    return res.rowcount or 0


async def claim_due(engine_pyp: AsyncEngine, *, limit: int = 32, lease_s: int = 300) -> list[dict]:
    """原子领取到期待投递（pending 且 next_retry_at 空或已过）行，置 inflight + 租约。

    单条 ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)``：多 Consumer 并发
    领取互不重复（DB-009），慢事务持有的行被直接跳过而非阻塞。返回已领取记录。
    """
    picked = (
        select(PushOutbox.id)
        .where(
            PushOutbox.state == OutboxState.PENDING.value,
            or_(PushOutbox.next_retry_at.is_(None), PushOutbox.next_retry_at <= func.now()),
        )
        .order_by(PushOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with engine_pyp.begin() as conn:
        rows = (
            await conn.execute(
                update(PushOutbox.__table__)
                .where(PushOutbox.id.in_(picked))
                .values(state=OutboxState.INFLIGHT.value, lease_until=datetime.now(UTC) + timedelta(seconds=lease_s))
                .returning(
                    PushOutbox.id,
                    PushOutbox.component_id,
                    PushOutbox.payload_ref,
                    PushOutbox.idempotency_key,
                    PushOutbox.attempts,
                )
            )
        ).all()
    rows = sorted(rows, key=lambda r: r[0])  # RETURNING 不保证顺序
    return [
        {"id": r[0], "component_id": r[1], "payload_ref": r[2], "idempotency_key": r[3], "attempts": r[4]} for r in rows
    ]


async def mark_sent(engine_pyp: AsyncEngine, outbox_id: int) -> None:
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(PushOutbox.__table__)
            # 状态守卫：仅 inflight 可终结——租约被回收又被他人重领时，迟到的成功不覆盖新状态
            .where(PushOutbox.id == outbox_id, PushOutbox.state == OutboxState.INFLIGHT.value)
            .values(state=OutboxState.SENT.value, lease_until=None, last_error=None)
        )


async def mark_failed(engine_pyp: AsyncEngine, outbox_id: int, *, error: str, attempts: int, max_attempts: int) -> str:
    """投递失败：attempts+1；未达上限 → pending + 退避 next_retry_at；达上限 → dead。返回新状态。"""
    new_attempts = attempts + 1
    if new_attempts >= max_attempts:
        return await mark_dead(engine_pyp, outbox_id, error=error, attempts=new_attempts)
    delay = backoff_delay(new_attempts, base=2.0, cap=300.0, jitter=True)
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(PushOutbox.__table__)
            .where(PushOutbox.id == outbox_id, PushOutbox.state == OutboxState.INFLIGHT.value)
            .values(
                state=OutboxState.PENDING.value,
                attempts=new_attempts,
                next_retry_at=datetime.now(UTC) + timedelta(seconds=delay),
                lease_until=None,
                last_error=error[:2000],
            )
        )
    return OutboxState.PENDING.value


async def mark_dead(engine_pyp: AsyncEngine, outbox_id: int, *, error: str, attempts: int) -> str:
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(PushOutbox.__table__)
            .where(PushOutbox.id == outbox_id, PushOutbox.state == OutboxState.INFLIGHT.value)
            .values(state=OutboxState.DEAD.value, attempts=attempts, lease_until=None, last_error=error[:2000])
        )
    return OutboxState.DEAD.value


async def requeue_expired(engine_pyp: AsyncEngine) -> int:
    """Consumer 崩溃恢复：inflight 且租约到期 → 回 pending（下轮重投）。返回回收条数。"""
    async with engine_pyp.begin() as conn:
        res = await conn.execute(
            update(PushOutbox.__table__)
            .where(
                PushOutbox.state == OutboxState.INFLIGHT.value,
                PushOutbox.lease_until.is_not(None),
                PushOutbox.lease_until < func.now(),
            )
            .values(state=OutboxState.PENDING.value, lease_until=None)
        )
    return res.rowcount or 0


async def run_outbox_once(
    engine_pyp: AsyncEngine,
    deliverer: Deliverer,
    *,
    max_attempts: int = 5,
    lease_s: int = 300,
    limit: int = 32,
) -> tuple[int, int]:
    """一轮排空：回收过期租约 → 领取到期 → 逐条投递 → sent | 退避/死信。返回 (成功数, 失败数)。"""
    await requeue_expired(engine_pyp)
    rows = await claim_due(engine_pyp, limit=limit, lease_s=lease_s)
    sent = failed = 0
    for row in rows:
        try:
            await deliverer(row)
        except Exception as exc:  # noqa: BLE001 —— 单条投递失败不拖垮整轮；退避/死信
            await mark_failed(
                engine_pyp, row["id"], error=str(exc), attempts=row["attempts"], max_attempts=max_attempts
            )
            failed += 1
        else:
            await mark_sent(engine_pyp, row["id"])
            sent += 1
    return sent, failed
