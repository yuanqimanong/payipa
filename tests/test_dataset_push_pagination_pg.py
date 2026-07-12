"""回归（需 PG）：dataset 型 outbox 载荷解析必须投递**整表**，而非静默只投首页。

此前 `_resolve_payload` 的 dataset 分支只调一次 read_dataset(limit=1000) 并丢弃 next_after，
「自动/链路触发的整表推送」会静默丢掉首页之外的所有行。本测试固定其 keyset 翻页取全量的行为，
并验证「显式带 after_id/limit 时仍尊重单页语义」的向后兼容分支。
"""

from __future__ import annotations

import asyncio
import json

import payipa.deliver.component as component
from payipa.db.settings import get_settings
from payipa.deliver.component import _resolve_payload
from payipa.studio.asm import AsmLoader, build_asm_table, create_asm_table, drop_asm_table
from sqlalchemy.ext.asyncio import create_async_engine

_PROD = "dspage"


def test_dataset_payload_paginates_full_table(require_pg: None, monkeypatch) -> None:
    # 用小页尺寸逼出多页翻页（默认 1000 行一页，小数据集难覆盖循环）
    monkeypatch.setattr(component, "_DATASET_PAGE_ROWS", 2)
    asm = build_asm_table(_PROD, [])

    async def scenario() -> None:
        biz = create_async_engine(get_settings().async_url("business"))
        try:
            await drop_asm_table(biz, asm)
            await create_asm_table(biz, asm)
            await AsmLoader(biz).upsert(asm, [{"title": f"t{i}", "n": i} for i in range(5)], fingerprint_keys=["title"])

            # ① 未显式分页 → 翻页取全量 5 行（跨 3 页：2+2+1），而非只投首页 2 行
            full = await _resolve_payload(biz, json.dumps({"kind": "dataset", "product_code": _PROD}))
            assert len(full) == 5, f"整表推送应返回全部 5 行，实际 {len(full)}"
            assert {r["title"] for r in full} == {f"t{i}" for i in range(5)}

            # ② 显式 limit → 尊重单页语义（向后兼容），只返回该页
            one = await _resolve_payload(biz, json.dumps({"kind": "dataset", "product_code": _PROD, "limit": 2}))
            assert len(one) == 2, f"显式 limit=2 应只返回单页 2 行，实际 {len(one)}"
        finally:
            await drop_asm_table(biz, asm)
            await biz.dispose()

    asyncio.run(scenario())
