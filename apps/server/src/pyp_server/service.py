"""server 侧编排：建源→存规则→建表→建批次（派发交给后台派发环 scheduler.dispatch_loop）。"""

from __future__ import annotations

from payipa.crawl.rules import RuleStore
from payipa.crawl.run import create_batch_with_requests, ensure_data_table, setup_source
from payipa.db.engine import get_engine
from payipa_contracts import Channel, RulePack


async def dispatch_source_run(
    *,
    uuid: str,
    name: str,
    seed_urls: list[str],
    rule: RulePack,
    indexed_fields: list[str] | None = None,
    channel: Channel = Channel.PROD,
    access_basis: str | None = None,
    access_reference: str | None = None,
    access_confirmed: bool = False,
) -> dict:
    """建源+存规则+建表+建批次；请求以 QUEUED 落库，实际下发由后台派发环负责。

    返回 {batch_id, requests, dispatched}；``dispatched`` 恒为 0——派发不再在此同步发生，
    避免「空闲槽不够就丢请求」的一次性派发缺陷（M1 遗留）。
    """
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    source_id, task_id = await setup_source(
        pyp,
        uuid,
        name,
        seed_urls=seed_urls,
        access_basis=access_basis,
        access_reference=access_reference,
        access_confirmed=access_confirmed,
    )
    ptr = await RuleStore(pyp).put(source_id, rule)
    fields_indexed = indexed_fields or [f.name for f in rule.fields if f.index]
    await ensure_data_table(dc, uuid, fields_indexed)
    batch_id, specs = await create_batch_with_requests(
        pyp, task_id=task_id, source_uuid=uuid, targets=seed_urls, rule_ptr=ptr, channel=channel
    )
    return {"batch_id": batch_id, "requests": len(specs), "dispatched": 0}
