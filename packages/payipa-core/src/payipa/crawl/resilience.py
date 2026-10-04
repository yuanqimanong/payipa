"""采集韧性：请求重试预算、Retry-After 与整源冷却。"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from jianbing_utils.retry import backoff_delay
from payipa_contracts import (
    ErrorCode,
    RequestState,
)
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import Batch, Request, Source, Task, TaskEvent

_RETRYABLE_ERRORS = frozenset(
    {int(ErrorCode.NETWORK), int(ErrorCode.TIMEOUT), int(ErrorCode.THROTTLED), int(ErrorCode.UPSTREAM)}
)


def _retry_delay(error_code: int, attempt: int, requested_s: float | None) -> int:
    """计算受控退避：尊重 Retry-After，但统一限制在 1 秒到 1 小时。"""
    if requested_s is not None:
        return min(3600, max(1, math.ceil(requested_s)))
    base = 30 if error_code == int(ErrorCode.THROTTLED) else 5
    return int(backoff_delay(min(max(attempt, 0), 6) + 1, base=base, cap=300.0))


async def defer_request_for_retry(
    engine_pyp: AsyncEngine,
    req_id: int,
    error_code: int,
    *,
    retry_after_s: float | None = None,
    response_status: int | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    max_attempt: int = 3,
    agent_id: str | None = None,
    attempt: int | None = None,
) -> tuple[str | None, bool]:
    """把可恢复失败延迟重排；达到源/全局上限则定格失败。

    返回 ``(source_uuid, requeued)``。429/5xx 同时设置源级冷却，确保同源其他请求也不会在
    Retry-After 窗口内抢跑；网络类故障只延迟当前请求。
    传 agent_id/attempt 时按 fencing 守卫（P0-10）：迟到/越权失败回报不消耗新代次的重试预算。
    """
    if error_code not in _RETRYABLE_ERRORS:
        raise ValueError(f"error_code {error_code} 不是可重试错误")
    now = datetime.now(UTC)
    async with engine_pyp.begin() as conn:
        row = (
            await conn.execute(
                select(
                    Request.attempt,
                    Request.state,
                    Request.agent_id,
                    Request.batch_id,
                    Source.id.label("source_id"),
                    Source.uuid,
                    Source.retry,
                )
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            return None, False
        if row.state not in {int(RequestState.ASSIGNED), int(RequestState.RUNNING)}:
            # 重复/迟到回报不得再次消耗重试预算。已经回队则维持“已重排”语义。
            return str(row.uuid), row.state == int(RequestState.QUEUED)
        if agent_id is not None and row.agent_id != agent_id:
            return str(row.uuid), True  # 越权/迟到（请求已重派给他人）：不动新代次
        if attempt is not None and int(row.attempt or 0) != attempt:
            return str(row.uuid), True  # 代次不符：旧代次的失败不消耗新代次预算
        next_attempt = int(row.attempt or 0) + 1
        attempt_limit = max(1, min(max_attempt, int(row.retry or max_attempt)))
        delay_s = _retry_delay(error_code, int(row.attempt or 0), retry_after_s)
        not_before = now + timedelta(seconds=delay_s)
        requeued = next_attempt < attempt_limit
        detail = (message or "")[:1000] or None
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(
                state=int(RequestState.QUEUED) if requeued else error_code,
                attempt=next_attempt,
                not_before=not_before if requeued else None,
                lease_until=None,
                agent_id=None,
                error_code=error_code,
                response_status=response_status,
                reason_code=(reason_code or "retryable_failure")[:64],
                error_detail=detail,
                retry_after_s=delay_s,
            )
        )
        source_values: dict = {
            "last_status_code": response_status,
            "last_failure_at": func.now(),
            "consecutive_failures": Source.consecutive_failures + 1,
        }
        if error_code in {int(ErrorCode.THROTTLED), int(ErrorCode.UPSTREAM)}:
            extend_cooldown = (Source.cooldown_until.is_(None)) | (Source.cooldown_until < not_before)
            source_values.update(
                cooldown_until=case((extend_cooldown, not_before), else_=Source.cooldown_until),
                cooldown_reason=case((extend_cooldown, (reason_code or "backoff")[:64]), else_=Source.cooldown_reason),
            )
        await conn.execute(update(Source.__table__).where(Source.id == row.source_id).values(**source_values))
        await conn.execute(
            TaskEvent.__table__.insert().values(
                batch_id=row.batch_id,
                type="request.deferred" if requeued else "request.retry_exhausted",
                payload={
                    "req_id": req_id,
                    "error_code": error_code,
                    "reason_code": reason_code,
                    "response_status": response_status,
                    "retry_after_s": delay_s,
                    "attempt": next_attempt,
                },
            )
        )
    return str(row.uuid), requeued
