"""组装运行编排（M3 首刀，Local 执行器）：建产物表 → 执行组装脚本（经 Gateway 取数）→ 幂等装载 asm_*。

真 SandboxExecutor / job_token / HTTP+Arrow 网关 / 增量水位 / 自动触发 在后续 M3 切片接线；此处坐实
「data_* → Query Gateway → assemble(ctx) → asm_*」一条最小可验证主链。跨库只由 core 编排，脚本不接触 DB。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.studio.asm import AsmLoader, build_asm_table, create_asm_table
from payipa.studio.executor import AssembleContext, AssembleFn, CodeExecutor, LocalExecutor
from payipa.studio.gateway import QueryGateway


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
) -> int:
    """跑一次组装：确保 asm_{product_code} 表在 → 执行脚本产出字段行 → 指纹幂等 upsert。返回写入行数。"""
    table = build_asm_table(product_code, indexed_fields)
    await create_asm_table(engine_business, table)
    ctx = AssembleContext(engine_dc, QueryGateway())
    rows = await (executor or LocalExecutor()).run(script, ctx)
    return await AsmLoader(engine_business).upsert(
        table, rows, assembly_id=assembly_id, fingerprint_keys=fingerprint_keys
    )
