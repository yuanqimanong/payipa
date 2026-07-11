"""agent 回传的结构化结果（小 item）+ 工件指针 + 执行摘要（含数据质量计数）。

FieldMeta（汲取点①）：每字段带证据链，支持**字段级降级**（单字段低置信/失败不丢整条记录）；
直接支撑 monitor 的解析成功·失败·空白率统计。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from payipa_contracts._annotate import active
from payipa_contracts.artifact import ArtifactRef


class FieldMeta(BaseModel):
    """单字段的清洗证据链。"""

    raw_value: str | None = active("提取到的原文（保真，可重解析）", default=None)
    normalized_value: Any | None = active("清洗/归一后的值", default=None)
    confidence: float | None = active("置信度 0–1（低置信标记但不丢记录）", default=None, ge=0, le=1)
    locator: str | None = active("命中定位器（排查用）", default=None)
    warnings: list[str] = active("字段级告警（降级原因等）", default_factory=list)


class Item(BaseModel):
    """一条抓取记录：用户字段袋 + 每字段证据链。"""

    fields: dict[str, Any] = active("用户字段键值（入库落 data_*.fields JSONB）", default_factory=dict)
    field_meta: dict[str, FieldMeta] = active("每字段的 FieldMeta", default_factory=dict)


class ExecSummary(BaseModel):
    """一次执行的摘要（喂 batch 统计与 monitor 数据质量）。"""

    elapsed_s: float = active("耗时（秒）", ge=0)
    count_ok: int = active("解析成功条数", default=0, ge=0)
    count_fail: int = active("解析失败条数", default=0, ge=0)
    count_blank: int = active("空白（无内容）条数", default=0, ge=0)
    warnings: list[str] = active("执行级告警", default_factory=list)
    response_status: int | None = active(
        "最终 HTTP 状态码（无 HTTP 响应时为空）", default=None, ge=100, le=599, since="M6"
    )
    response_bytes: int = active("响应正文大小（字节）", default=0, ge=0, since="M6")
    engine: str | None = active("实际使用的采集引擎", default=None, max_length=32, since="M6")


class ResultBatch(BaseModel):
    """一个请求任务的回传结果（结构化结果走控制面，大对象走数据面回指针）。"""

    batch_id: str = active("所属批次 id（correlation）")
    req_id: str = active("请求数据任务 id（correlation）")
    items: list[Item] = active("结构化记录（小 item）", default_factory=list)
    artifacts: list[ArtifactRef] = active("大对象指针（raw/多媒体）", default_factory=list)
    discovered: list[str] = active(
        "本页发现的待跟进链接（link/store+link 字段值）；主控 URL 指纹去重后并入同批入队，深度=父+1、受 max_depth 限",
        default_factory=list,
        since="M2",
    )
    summary: ExecSummary = active("执行摘要")
