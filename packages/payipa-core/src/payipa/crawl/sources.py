"""数据源生命周期、访问复核、限流信息与动态表 provisioning。"""

from __future__ import annotations

from collections.abc import Sequence

from payipa_contracts import (
    Channel,
    EngineHint,
    ErrorCode,
    RequestState,
)
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl._policy import _INFLIGHT, _validate_access_record
from payipa.crawl.ingest import build_data_table, create_data_table
from payipa.db.dynamic_schema import provision_data_schema
from payipa.db.ident import check_code
from payipa.db.pyp import Batch, Request, Rule, Source, Task, TaskEvent


async def setup_source(
    engine_pyp: AsyncEngine,
    uuid: str,
    name: str = "M1 source",
    *,
    seed_urls: Sequence[str] | None = None,
    access_basis: str | None = None,
    access_reference: str | None = None,
    access_confirmed: bool = False,
    engine_hint: EngineHint | None = None,
    rate_limit: int | None = None,
    retry: int | None = None,
    timeout: int | None = None,
    raw_archive: bool | None = None,
) -> tuple[int, int]:
    """确保已确认访问依据的 source + 一个 task 存在；返回 (source_id, task_id)。

    seed_urls 存档进 task.params（最近一次为准），供 cron/重跑无需重新提交种子（07 定时触发）。
    新数据源必须显式确认访问依据；既有数据源一旦暂停，只能经人工复核接口恢复。
    """
    check_code(uuid)  # 短码进 data_{uuid} 分表名/对象存储 key，落库前先过统一校验（P0-13）
    if rate_limit is not None and not 1 <= rate_limit <= 1000:
        raise ValueError("rate_limit 必须在 1–1000 req/s 之间")
    if retry is not None and not 1 <= retry <= 10:
        raise ValueError("retry 必须在 1–10 次之间")
    if timeout is not None and not 5 <= timeout <= 1800:
        raise ValueError("timeout 必须在 5–1800 秒之间")
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(Source.id, Source.access_confirmed_at, Source.paused_at).where(Source.uuid == uuid)
            )
        ).first()
        if source is None:
            if not access_confirmed:
                raise PermissionError("新数据源必须由操作者确认访问授权")
            basis, reference = _validate_access_record(access_basis, access_reference)
            source_id = (
                await conn.execute(
                    pg_insert(Source.__table__)
                    .values(
                        uuid=uuid,
                        name=name,
                        connector_type="web",
                        access_basis=basis,
                        access_reference=reference,
                        access_confirmed_at=func.now(),
                        rate_limit=rate_limit if rate_limit is not None else 10,
                        retry=retry if retry is not None else 3,
                        timeout=timeout if timeout is not None else 30,
                        raw_archive=bool(raw_archive),
                    )
                    .returning(Source.id)
                )
            ).scalar_one()
        else:
            source_id, confirmed_at, paused_at = source
            if confirmed_at is None:
                raise PermissionError("数据源尚未完成人工访问授权复核")
            if paused_at is not None:
                raise PermissionError("数据源已暂停，须完成人工复核后才能恢复")
            source_values: dict = {"name": name}
            for key, value in {
                "rate_limit": rate_limit,
                "retry": retry,
                "timeout": timeout,
                "raw_archive": raw_archive,
            }.items():
                if value is not None:
                    source_values[key] = value
            await conn.execute(update(Source.__table__).where(Source.id == source_id).values(**source_values))
        task_row = (
            await conn.execute(select(Task.id, Task.params).where(Task.source_id == source_id).limit(1))
        ).first()
        params = dict(task_row.params or {}) if task_row is not None else {}
        if seed_urls:
            params["seed_urls"] = list(seed_urls)
        if engine_hint is not None:
            params["engine_hint"] = engine_hint.value
        elif task_row is None:
            params["engine_hint"] = EngineHint.HTTP.value
        if task_row is None:
            task_id = (
                await conn.execute(
                    pg_insert(Task.__table__)
                    .values(source_id=source_id, trigger_type="manual", params=params)
                    .returning(Task.id)
                )
            ).scalar_one()
        else:
            task_id = task_row.id
            await conn.execute(update(Task.__table__).where(Task.id == task_id).values(params=params))
    return source_id, task_id


async def review_source_access(
    engine_pyp: AsyncEngine,
    uuid: str,
    *,
    access_basis: str,
    access_reference: str,
    approved: bool,
    reason: str | None = None,
) -> bool:
    """记录人工访问复核。批准会恢复调度；拒绝会保持整源暂停。"""
    basis, reference = _validate_access_record(access_basis, access_reference)
    values: dict = {
        "access_basis": basis,
        "access_reference": reference,
        "access_confirmed_at": func.now() if approved else None,
        "paused_at": None if approved else func.now(),
        "pause_reason": None if approved else (reason or "人工访问复核未通过")[:1000],
        "cooldown_until": None,
        "cooldown_reason": None,
        "consecutive_failures": 0 if approved else Source.consecutive_failures,
    }
    async with engine_pyp.begin() as conn:
        result = await conn.execute(update(Source.__table__).where(Source.uuid == uuid).values(**values))
    return bool(result.rowcount)


async def ensure_data_table(
    engine_dc: AsyncEngine,
    source_uuid: str,
    indexed_fields: Sequence[str] = (),
    *,
    engine_pyp: AsyncEngine | None = None,
    channel: Channel | str = Channel.PROD,
) -> Table:
    """建源时程序化建 data_{uuid} 表（幂等）。"""
    if engine_pyp is not None:
        return await provision_data_schema(
            engine_pyp,
            engine_dc,
            source_uuid,
            indexed_fields,
            channel=channel,
        )
    table = build_data_table(source_uuid, indexed_fields, channel)
    await create_data_table(engine_dc, table)
    return table


async def source_rate_limits(engine_pyp: AsyncEngine) -> dict[str, int]:
    """有 running 批次的各源的 rate_limit（req/s）：{source_uuid: rate_limit}，供派发环限流。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(Source.uuid, Source.rate_limit)
                .select_from(Source.__table__)
                .join(Task.__table__, Task.source_id == Source.id)
                .join(Batch.__table__, Batch.task_id == Task.id)
                .where(
                    Batch.status == "running",
                    Source.access_confirmed_at.is_not(None),
                    Source.paused_at.is_(None),
                    (Source.cooldown_until.is_(None)) | (Source.cooldown_until <= func.now()),
                )
                .distinct()
            )
        ).all()
    return {u: int(r) for u, r in rows}


async def source_of_request(engine_pyp: AsyncEngine, req_id: int) -> str | None:
    """由 req_id 反解数据源短码（供 AIMD 回退信号定位数据源）。"""
    async with engine_pyp.connect() as conn:
        return (
            await conn.execute(
                select(Source.uuid)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
            )
        ).scalar()


async def pause_source_for_request(
    engine_pyp: AsyncEngine,
    req_id: int,
    reason: str | None = None,
    *,
    response_status: int | None = None,
    reason_code: str | None = None,
) -> tuple[str | None, list[str], list[int]]:
    """因访问拒绝暂停整源，并终止该源所有尚未派发的请求。

    返回 ``(source_uuid, 其他在途 req_id, running batch_id)``；调用方负责取消在途任务并收尾已无在途请求的批次。
    """
    message = (reason or "目标系统拒绝访问，等待人工复核")[:1000]
    async with engine_pyp.begin() as conn:
        source = (
            await conn.execute(
                select(Source.id, Source.uuid, Request.batch_id)
                .select_from(Request.__table__)
                .join(Batch.__table__, Request.batch_id == Batch.id)
                .join(Task.__table__, Batch.task_id == Task.id)
                .join(Source.__table__, Task.source_id == Source.id)
                .where(Request.id == req_id)
            )
        ).first()
        if source is None:
            return None, [], []
        source_id, source_uuid, trigger_batch_id = source
        batch_ids = list(
            (
                await conn.execute(
                    select(Batch.id)
                    .join(Task.__table__, Batch.task_id == Task.id)
                    .where(Task.source_id == source_id, Batch.status == "running")
                )
            )
            .scalars()
            .all()
        )
        other_inflight = list(
            (
                await conn.execute(
                    select(Request.id).where(
                        Request.batch_id.in_(batch_ids),
                        Request.state.in_(_INFLIGHT),
                        Request.id != req_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            update(Source.__table__)
            .where(Source.id == source_id)
            .values(
                paused_at=func.now(),
                pause_reason=message,
                cooldown_until=None,
                cooldown_reason=None,
                last_status_code=response_status,
                last_failure_at=func.now(),
                consecutive_failures=Source.consecutive_failures + 1,
            )
        )
        await conn.execute(
            update(Request.__table__)
            .where(
                Request.batch_id.in_(batch_ids),
                (Request.state == int(RequestState.QUEUED)) | (Request.id == req_id),
            )
            .values(
                state=int(ErrorCode.ACCESS_PAUSED),
                error_code=int(ErrorCode.ACCESS_PAUSED),
                lease_until=None,
                not_before=None,
            )
        )
        await conn.execute(
            update(Request.__table__)
            .where(Request.id == req_id)
            .values(
                response_status=response_status,
                reason_code=(reason_code or "access_review_required")[:64],
                error_detail=message,
            )
        )
        await conn.execute(
            TaskEvent.__table__.insert().values(
                batch_id=trigger_batch_id,
                type="source.access_paused",
                payload={
                    "source": str(source_uuid),
                    "req_id": req_id,
                    "response_status": response_status,
                    "reason_code": reason_code,
                },
            )
        )
    return str(source_uuid), [str(value) for value in other_inflight], [int(value) for value in batch_ids]


async def source_field_names(engine_pyp: AsyncEngine, source_uuid: str) -> list[str]:
    """数据源当前规则声明的字段名（供 CSV 导出的稳定列序）；无规则返回空列表。"""
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(Rule.spec)
                .join(Source.__table__, Rule.source_id == Source.id)
                .where(Source.uuid == source_uuid)
                .order_by(Rule.version.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return []
    try:
        return [f["name"] for f in (row[0] or {}).get("fields", []) if f.get("name")]
    except AttributeError, TypeError:
        return []
