"""动态 data_*/asm_* 表的 pyp_sys 台账与跨库 provisioning 对账。"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

from payipa_contracts import Channel, RulePack
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl.ingest import build_data_table, create_data_table
from payipa.db.ident import check_code, check_field, check_ident
from payipa.db.pyp import DynamicSchema, Rule, Source


async def mark_source_provisioning(
    engine_pyp: AsyncEngine, source_uuid: str, state: str, error: str | None = None
) -> None:
    if state not in {"ready", "provisioning", "error"}:
        raise ValueError(f"invalid provisioning state: {state}")
    async with engine_pyp.begin() as conn:
        await conn.execute(
            update(Source.__table__)
            .where(Source.uuid == source_uuid)
            .values(provisioning_state=state, provisioning_error=(error or None))
        )


async def record_dynamic_schema(
    engine_pyp: AsyncEngine,
    *,
    kind: str,
    object_code: str,
    database_name: str,
    table_name: str,
    indexed_fields: Sequence[str],
    status: str,
    channel: Channel | str = Channel.PROD,
    error: str | None = None,
) -> None:
    if kind not in {"data", "assembly"}:
        raise ValueError(f"invalid dynamic schema kind: {kind}")
    if status not in {"ready", "provisioning", "error"}:
        raise ValueError(f"invalid dynamic schema status: {status}")
    code = check_code(object_code)
    channel_value = Channel(channel).value
    database = check_ident(database_name)
    table = check_ident(table_name)
    fields = sorted({check_field(value) for value in indexed_fields})
    values = {
        "kind": kind,
        "object_code": code,
        "channel": channel_value,
        "database_name": database,
        "table_name": table,
        "indexed_fields": fields,
        "status": status,
        "last_error": (error or None),
    }
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(DynamicSchema.__table__)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["kind", "object_code", "channel"],
                set_={**values, "updated_at": func.now()},
            )
        )


async def provision_data_schema(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    source_uuid: str,
    indexed_fields: Sequence[str],
    *,
    channel: Channel | str = Channel.PROD,
):
    """将 data_* provisioning 的意图、DDL 结果和错误写成可重试状态。"""
    code = None
    table = None
    fields: list[str] = []
    channel_value = Channel(channel)
    try:
        code = check_code(source_uuid)
        fields = sorted({check_field(value) for value in indexed_fields})
        table = build_data_table(code, fields, channel_value)
        if channel_value is Channel.PROD:
            await mark_source_provisioning(engine_pyp, code, "provisioning")
        await record_dynamic_schema(
            engine_pyp,
            kind="data",
            object_code=code,
            database_name="data_center",
            table_name=table.name,
            indexed_fields=fields,
            status="provisioning",
            channel=channel_value,
        )
        await create_data_table(engine_dc, table)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:2000]
        if channel_value is Channel.PROD:
            with contextlib.suppress(Exception):
                await mark_source_provisioning(engine_pyp, source_uuid, "error", message)
        if code is not None and table is not None:
            with contextlib.suppress(Exception):
                await record_dynamic_schema(
                    engine_pyp,
                    kind="data",
                    object_code=code,
                    database_name="data_center",
                    table_name=table.name,
                    indexed_fields=fields,
                    status="error",
                    channel=channel_value,
                    error=message,
                )
        raise
    await record_dynamic_schema(
        engine_pyp,
        kind="data",
        object_code=code,
        database_name="data_center",
        table_name=table.name,
        indexed_fields=fields,
        status="ready",
        channel=channel_value,
    )
    if channel_value is Channel.PROD:
        await mark_source_provisioning(engine_pyp, code, "ready")
    return table


async def reconcile_data_schemas(
    engine_pyp: AsyncEngine,
    engine_dc: AsyncEngine,
    *,
    source_uuid: str | None = None,
) -> dict[str, int]:
    """重试所有非 ready 数据源；逐源隔离失败，供后台循环周期调用。"""
    query = select(Source.id, Source.uuid).where(Source.provisioning_state != "ready")
    if source_uuid is not None:
        query = query.where(Source.uuid == source_uuid)
    async with engine_pyp.connect() as conn:
        sources = (await conn.execute(query.order_by(Source.id))).all()
    repaired = failed = 0
    for source_id, code in sources:
        async with engine_pyp.connect() as conn:
            spec = (
                await conn.execute(
                    select(Rule.spec).where(Rule.source_id == source_id).order_by(Rule.version.desc()).limit(1)
                )
            ).scalar()
        try:
            if spec is None:
                raise LookupError("no rule available for provisioning")
            pack = RulePack.model_validate(spec)
            await provision_data_schema(engine_pyp, engine_dc, code, [f.name for f in pack.fields if f.index])
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            await mark_source_provisioning(engine_pyp, code, "error", message)
            failed += 1
        else:
            repaired += 1
    return {"checked": len(sources), "repaired": repaired, "failed": failed}


async def dynamic_schema_health(engine_pyp: AsyncEngine) -> tuple[int, int]:
    async with engine_pyp.connect() as conn:
        provisioning = (
            await conn.execute(
                select(func.count()).select_from(Source.__table__).where(Source.provisioning_state == "provisioning")
            )
        ).scalar() or 0
        failed = (
            await conn.execute(
                select(func.count()).select_from(Source.__table__).where(Source.provisioning_state == "error")
            )
        ).scalar() or 0
    return int(provisioning), int(failed)
