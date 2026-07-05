"""应用 REST API。M1：/api/agents（在线节点）+ /api/sources/{uuid}/run（手动触发单源一次）。

其余为契约 stub（让 OpenAPI 呈现 schema）。完整端点清单见 SDD §6.5。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from payipa.crawl.rules import RuleStore
from payipa.crawl.run import create_batch_with_requests, ensure_data_table, setup_source
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings
from payipa.security.tokens import issue_upload_token
from payipa_contracts import (
    BatchProgress,
    Channel,
    NodeSnapshot,
    QueueStat,
    RulePack,
    TaskAssign,
    TaskSpec,
)
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["api"])


class RunRequest(BaseModel):
    seed_urls: list[str] = Field(..., description="种子 URL 列表（每个产一条 request）")
    rule: RulePack = Field(..., description="本次运行使用的声明式规则")
    indexed_fields: list[str] = Field(default_factory=list, description="需索引字段（不填则取 rule 中 index=true）")
    channel: Channel = Channel.PROD


class RunResponse(BaseModel):
    batch_id: int
    requests: int
    dispatched: int


@router.get("/agents", response_model=list[NodeSnapshot], summary="在线节点快照（来自 AgentHub）")
async def list_agents(request: Request) -> list[NodeSnapshot]:
    return request.app.state.hub.snapshots()


@router.get("/monitor/queue", response_model=QueueStat, summary="队列统计（M0 空壳，M2 接线）")
async def queue_stat() -> QueueStat:
    return QueueStat()


@router.get("/monitor/batches/{batch_id}", response_model=BatchProgress, summary="批次进度（M0 空壳，M2 接线）")
async def batch_progress(batch_id: str) -> BatchProgress:
    return BatchProgress(total=0, ok=0, fail=0, running=0, pct=0.0)


@router.post("/tasks/preview", response_model=TaskAssign, summary="校验并回显 TaskSpec（演示契约，M0）")
async def preview_task(spec: TaskSpec) -> TaskAssign:
    return TaskAssign(task=spec)


@router.post("/sources/{uuid}/run", response_model=RunResponse, summary="M1：手动触发单源一次采集")
async def run_source(uuid: str, body: RunRequest, request: Request) -> RunResponse:
    hub = request.app.state.hub
    pyp = get_engine("pyp")
    dc = get_engine("data_center")
    _source_id, task_id = await setup_source(pyp, uuid)
    ptr = await RuleStore(pyp).put(_source_id, body.rule)
    indexed = body.indexed_fields or [f.name for f in body.rule.fields if f.index]
    await ensure_data_table(dc, uuid, indexed)
    batch_id, specs = await create_batch_with_requests(
        pyp, task_id=task_id, source_uuid=uuid, targets=body.seed_urls, rule_ptr=ptr, channel=body.channel
    )
    secret = get_settings().upload_secret
    dispatched = 0
    for spec in specs:
        conn = hub.pick_free()
        if conn is None:  # 无空闲 agent：留排队（M2 由调度循环重新派发）
            break
        token = issue_upload_token(secret, uuid, batch_id)
        await hub.send_frame(conn.agent_id, TaskAssign(task=spec, upload_token=token))
        hub.on_dispatched(conn.agent_id, spec.req_id)
        dispatched += 1
    return RunResponse(batch_id=batch_id, requests=len(specs), dispatched=dispatched)
