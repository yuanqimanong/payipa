"""组装运行编排：建产物表 → 执行组装脚本（经 Gateway 取数）→ 幂等装载 asm_*。

增量水位（slice-8）：传 engine_pyp + assembly_id + incremental=True 时，先读该组装各源水位交给 ctx，脚本
增量取数，组装成功 upsert 后把读到的最大 id 推进回水位（读腿可重算、写腿指纹幂等）。
执行面两口径（slice-6）：run_assembly=进程内 LocalExecutor（受信降级）；run_assembly_sandboxed=
锁定容器 + job_token 数据面（生产口径）。跨库只由 core 编排，脚本不接触 DB。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.studio.asm import AsmLoader, build_asm_table
from payipa.studio.evolve import evolve_asm_table
from payipa.studio.executor import AssembleContext, AssembleFn, CodeExecutor, LocalExecutor
from payipa.studio.gateway import QueryGateway
from payipa.studio.sandbox import SandboxExecutor
from payipa.studio.watermark import advance_watermarks, get_watermarks


async def run_assembly(
    engine_dc: AsyncEngine,
    engine_business: AsyncEngine,
    *,
    product_code: str,
    script: AssembleFn,
    assembly_id: int | None = None,
    fingerprint_keys: Sequence[str] = (),
    indexed_fields: Sequence[str] = (),
    executor: CodeExecutor | None = None,
    engine_pyp: AsyncEngine | None = None,
    incremental: bool = False,
) -> int:
    """跑一次组装：确保 asm_{product_code} 表在 → 执行脚本产出字段行 → 指纹幂等 upsert。返回写入行数。

    incremental=True（需 engine_pyp + assembly_id）：脚本用 ctx.read_table(incremental=True) 只读水位后的增量，
    upsert 成功后推进水位。水位前进只发生在组装成功之后，故中途失败下次会重读（读腿可重算 + 写腿幂等）。
    """
    table = build_asm_table(product_code, indexed_fields)
    await evolve_asm_table(engine_business, table)  # 建表或加法演进（新增索引字段自动加列；破坏性变更会拦截）
    watermarks: dict[str, int] = {}
    if incremental and engine_pyp is not None and assembly_id is not None:
        watermarks = await get_watermarks(engine_pyp, assembly_id)
    ctx = AssembleContext(engine_dc, QueryGateway(), watermarks=watermarks)
    rows = await (executor or LocalExecutor()).run(script, ctx)
    written = await AsmLoader(engine_business).upsert(
        table, rows, assembly_id=assembly_id, fingerprint_keys=fingerprint_keys
    )
    if incremental and engine_pyp is not None and assembly_id is not None and ctx.new_watermarks:
        await advance_watermarks(engine_pyp, assembly_id, ctx.new_watermarks)
    return written


async def run_assembly_sandboxed(
    engine_business: AsyncEngine,
    *,
    product_code: str,
    script_source: str,
    sources: Sequence[str],
    sandbox: SandboxExecutor,
    assembly_id: int | None = None,
    fingerprint_keys: Sequence[str] = (),
    indexed_fields: Sequence[str] = (),
    engine_pyp: AsyncEngine | None = None,
    incremental: bool = False,
    row_quota: int | None = None,
    job_id: str | None = None,
) -> int:
    """沙箱口径跑一次组装（M3 slice-6）：脚本**源码**进锁定容器执行、经 Query Gateway HTTP 取数。

    建表/演进、幂等装载与水位推进都留在可信父进程——容器只产出行，不接触任何 DB（红线2）。
    与 run_assembly 同编排语义，执行面从进程内换成容器 + job_token 数据面；水位约定同 slice-8
    （只在装载成功后推进，读腿可重算 + 写腿幂等）。
    """
    table = build_asm_table(product_code, indexed_fields)
    await evolve_asm_table(engine_business, table)
    watermarks: dict[str, int] | None = None
    if incremental and engine_pyp is not None and assembly_id is not None:
        watermarks = await get_watermarks(engine_pyp, assembly_id)
    rows, new_watermarks = await sandbox.run_source(
        script_source,
        job_id=job_id or f"asm-{assembly_id or product_code}",
        sources=list(sources),
        row_quota=row_quota,
        watermarks=watermarks,
    )
    written = await AsmLoader(engine_business).upsert(
        table, rows, assembly_id=assembly_id, fingerprint_keys=fingerprint_keys
    )
    if incremental and engine_pyp is not None and assembly_id is not None and new_watermarks:
        await advance_watermarks(engine_pyp, assembly_id, new_watermarks)
    return written
