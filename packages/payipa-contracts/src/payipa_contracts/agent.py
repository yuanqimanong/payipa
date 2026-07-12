"""agent ↔ 主控 WebSocket 帧（一条连接多路复用：注册/心跳/任务/状态/取消/错误）。

WS 只传小消息（KB 级）；大对象走数据面直传、回指针。帧用 ``type`` 字段做判别联合，
便于 server 端点分发与 OpenAPI 呈现。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from payipa_contracts._annotate import active, reserved
from payipa_contracts.artifact import ArtifactRef
from payipa_contracts.result import ResultBatch
from payipa_contracts.task import TaskSpec
from payipa_contracts.version import CONTRACT_VERSION


class Capabilities(BaseModel):
    """agent 注册上报的能力标记。"""

    automation: bool = active("真实启动浏览器探测通过后上报自动化能力", default=False, since="M5")
    engines: list[str] = active("支持的抓取引擎清单；主控据此过滤派发候选", default_factory=list, since="M5")


# ── 客户端（agent）→ 主控 ──────────────────────────────────────────────────
class RegisterReq(BaseModel):
    """注册请求（join token 在传输层/首帧头携带，不入本 schema）。"""

    type: Literal["register"] = "register"
    agent_id: str = active("节点 id（自报或主控分配）")
    hostname: str = active("主机名")
    capabilities: Capabilities = active("能力标记", default_factory=Capabilities)
    slot_n: int = active("并发槽容量 N（机器规格智能默认，UI 可改）", ge=0, since="M2")
    contract_version: int = active("agent 契约版本（握手校验）", default=CONTRACT_VERSION)


class Heartbeat(BaseModel):
    """心跳（15–30s）：带在途清单与空闲槽（槽位信用制流控）。"""

    type: Literal["heartbeat"] = "heartbeat"
    inflight: list[str] = active("在途请求任务 id 清单", default_factory=list, since="M2")
    free_slots: int = active("空闲槽数", default=0, ge=0, since="M2")
    metrics: dict[str, Any] = reserved("机器/运行指标（喂 monitor）", default_factory=dict, since="M5")


class TaskAck(BaseModel):
    """任务接收确认：agent 收到 TaskAssign 即回；主控据此把请求 ASSIGNED→RUNNING 并展成执行租约。"""

    type: Literal["task_ack"] = "task_ack"
    req_id: str = active("请求任务 id", since="M7")
    attempt: int = active("执行代次（回显 TaskSpec.attempt）", default=0, ge=0, since="M7")


class StatusReport(BaseModel):
    """请求任务状态回报。state：>=0 正常态(RequestState)，<0 错误码(ErrorCode)。"""

    type: Literal["status"] = "status"
    req_id: str = active("请求任务 id")
    state: int = active("状态：正数=正常态、负数=错误码", since="M1")
    attempt: int | None = active(
        "执行代次（fencing：回显 TaskSpec.attempt；空=帧未携带代次，主控退化为只按 agent 归属校验）",
        default=None,
        ge=0,
        since="M7",
    )
    result_ref: ArtifactRef | None = active("结果工件指针（大对象）", default=None, since="M1")
    message: str | None = active("附加说明（失败原因等）", default=None)
    response_status: int | None = active("目标端 HTTP 状态码（无响应时为空）", default=None, ge=100, le=599, since="M6")
    reason_code: str | None = active(
        "稳定、可聚合的机器原因码（不携带响应正文或凭证）", default=None, max_length=64, since="M6"
    )
    retry_after_s: float | None = active(
        "建议等待秒数；主控仍会施加上下限和最大重试次数", default=None, ge=0, le=86400, since="M6"
    )


class ErrorFrame(BaseModel):
    """错误帧。"""

    type: Literal["error"] = "error"
    code: int = active("错误码（见 errors.ErrorCode）")
    message: str = active("错误描述")
    req_id: str | None = active("关联请求任务 id（若有）", default=None)
    attempt: int | None = active(
        "执行代次（fencing：回显 TaskSpec.attempt；空=帧未携带代次）", default=None, ge=0, since="M7"
    )


class ResultReport(BaseModel):
    """结果回传帧：小结构化结果 + 大对象指针 + 执行摘要（走控制面，大 blob 已直传）。"""

    type: Literal["result"] = "result"
    result: ResultBatch = active("回传结果（items + artifacts 指针 + summary）", since="M1")


# ── 主控 → 客户端（agent）──────────────────────────────────────────────────
class RegisterAck(BaseModel):
    """注册应答：换取长期节点凭证 + 契约版本。"""

    type: Literal["register_ack"] = "register_ack"
    node_token: str = active("长期节点凭证（存 hash，明文仅首次入网下发；重连时为空串=沿用既有凭证）", default="")
    contract_version: int = active("主控契约版本", default=CONTRACT_VERSION)
    heartbeat_interval_s: int = active("心跳间隔建议（秒）", default=20, gt=0)


class TaskAssign(BaseModel):
    """任务下发（仅在 agent 有空闲槽时下发）。"""

    type: Literal["task_assign"] = "task_assign"
    task: TaskSpec = active("任务定义", since="M1")
    upload_token: str | None = active(
        "本任务的内部上传 token（local 兜底回传 raw 用；绑定 source+batch）", default=None, since="M1"
    )
    rule_token: str | None = active("读取本任务规则内容的短期 token（绑定 content_hash）", default=None, since="M8")


class ResultAck(BaseModel):
    """主控对 ResultReport 的持久化确认；Agent 收到后才删除本地 spool。"""

    type: Literal["result_ack"] = "result_ack"
    req_id: str = active("请求任务 id", since="M8")
    attempt: int = active("结果执行代次", ge=0, since="M8")
    accepted: bool = active("是否为当前权威代次；False 表示迟到结果已被安全丢弃", since="M8")


class Cancel(BaseModel):
    """取消：按批次或单请求；agent 收帧后本地取消工作协程树、优雅收尾回传已抓部分。"""

    type: Literal["cancel"] = "cancel"
    batch_id: str | None = active("取消整批", default=None, since="M2")
    req_id: str | None = active("取消单请求", default=None, since="M2")


# ── 判别联合（供 server 端点分发 / OpenAPI）────────────────────────────────
ClientFrame = Annotated[
    RegisterReq | Heartbeat | TaskAck | StatusReport | ResultReport | ErrorFrame,
    Field(discriminator="type"),
]
ServerFrame = Annotated[
    RegisterAck | TaskAssign | ResultAck | Cancel,
    Field(discriminator="type"),
]
