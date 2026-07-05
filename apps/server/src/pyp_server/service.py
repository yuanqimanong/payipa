"""server 侧编排：建源→存规则→建表→建批次→派发（供 /api 与建源表单共用）。"""

from __future__ import annotations

from payipa.crawl.rules import RuleStore
from payipa.crawl.run import create_batch_with_requests, ensure_data_table, setup_source
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings
from payipa.security.tokens import issue_upload_token
from payipa_contracts import Channel, RulePack, TaskAssign

from pyp_server.hub import AgentHub


async def dispatch_source_run(
    hub: AgentHub,
    *,
    uuid: str,
    name: str,
    seed_urls: list[str],
    rule: RulePack,
    indexed_fields: list[str] | None = None,
    channel: Channel = Channel.PROD,
) -> dict:
    """建源+存规则+建表+建批次，并把请求派发给有空闲槽的 agent。返回 {batch_id, requests, dispatched}。"""
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    source_id, task_id = await setup_source(pyp, uuid, name)
    ptr = await RuleStore(pyp).put(source_id, rule)
    fields_indexed = indexed_fields or [f.name for f in rule.fields if f.index]
    await ensure_data_table(dc, uuid, fields_indexed)
    batch_id, specs = await create_batch_with_requests(
        pyp, task_id=task_id, source_uuid=uuid, targets=seed_urls, rule_ptr=ptr, channel=channel
    )
    secret = get_settings().upload_secret
    dispatched = 0
    for spec in specs:
        conn = hub.pick_free()
        if conn is None:  # 无空闲 agent：留排队（M2 由调度循环重发）
            break
        token = issue_upload_token(secret, uuid, batch_id)
        await hub.send_frame(conn.agent_id, TaskAssign(task=spec, upload_token=token))
        hub.on_dispatched(conn.agent_id, spec.req_id)
        dispatched += 1
    return {"batch_id": batch_id, "requests": len(specs), "dispatched": dispatched}
