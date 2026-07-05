"""应用 REST API。/api/agents（在线节点）+ /api/sources/{uuid}/run（触发单源）+ 契约 stub。

完整端点清单见 SDD §6.5；JSON API 的细粒度鉴权（RBAC）留后续安全里程碑。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from payipa.crawl.run import batch_progress as compute_batch_progress
from payipa.crawl.run import queue_depth as compute_queue_depth
from payipa.db.engine import get_engine
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

from pyp_server.service import dispatch_source_run

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


@router.get("/monitor/queue", response_model=QueueStat, summary="队列统计（QUEUED 排队深度，实时）")
async def queue_stat() -> QueueStat:
    return QueueStat(by_priority=await compute_queue_depth(get_engine("pyp")))


@router.get("/monitor/batches/{batch_id}", response_model=BatchProgress, summary="批次进度（按 state 实时聚合）")
async def batch_progress(batch_id: int) -> BatchProgress:
    return BatchProgress(**await compute_batch_progress(get_engine("pyp"), batch_id))


@router.post("/tasks/preview", response_model=TaskAssign, summary="校验并回显 TaskSpec（演示契约，M0）")
async def preview_task(spec: TaskSpec) -> TaskAssign:
    return TaskAssign(task=spec)


@router.post("/sources/{uuid}/run", response_model=RunResponse, summary="触发单源一次采集")
async def run_source(uuid: str, body: RunRequest) -> RunResponse:
    result = await dispatch_source_run(
        uuid=uuid,
        name=uuid,
        seed_urls=body.seed_urls,
        rule=body.rule,
        indexed_fields=body.indexed_fields,
        channel=body.channel,
    )
    return RunResponse(**result)
