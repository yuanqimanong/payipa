"""应用 REST API。/api/agents（在线节点）+ /api/sources/{uuid}/run（触发单源）+ 契约 stub。

完整端点清单见 SDD §6.5；JSON API 的细粒度鉴权（RBAC）留后续安全里程碑。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
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


@router.get("/monitor/queue", response_model=QueueStat, summary="队列统计（M0 空壳，M2 接线）")
async def queue_stat() -> QueueStat:
    return QueueStat()


@router.get("/monitor/batches/{batch_id}", response_model=BatchProgress, summary="批次进度（M0 空壳，M2 接线）")
async def batch_progress(batch_id: str) -> BatchProgress:
    return BatchProgress(total=0, ok=0, fail=0, running=0, pct=0.0)


@router.post("/tasks/preview", response_model=TaskAssign, summary="校验并回显 TaskSpec（演示契约，M0）")
async def preview_task(spec: TaskSpec) -> TaskAssign:
    return TaskAssign(task=spec)


@router.post("/sources/{uuid}/run", response_model=RunResponse, summary="触发单源一次采集")
async def run_source(uuid: str, body: RunRequest, request: Request) -> RunResponse:
    result = await dispatch_source_run(
        request.app.state.hub,
        uuid=uuid,
        name=uuid,
        seed_urls=body.seed_urls,
        rule=body.rule,
        indexed_fields=body.indexed_fields,
        channel=body.channel,
    )
    return RunResponse(**result)
