"""字段「已生效/未生效」标注助手（架构红线 00 §3.8：契约字段必须诚实）。

两种呈现都做（M0 定案）：
1. description 文字前缀 ``[已生效]`` / ``[未生效]``——人看 Swagger 直接可见；
2. ``json_schema_extra`` 结构化 ``{"x-effective": bool, "since_milestone": ...}``——机器/AI 读 OpenAPI 可解析。

``since_milestone`` 表示该字段语义**预计在哪个里程碑被平台逻辑真正消费**（M0 契约就位 ≠ 已接线）。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field


def active(description: str, *, since: str = "M1", **kwargs: Any) -> Any:
    """已生效字段：语义已（或将于 ``since`` 里程碑）被平台逻辑消费。"""
    return Field(
        description=f"[已生效] {description}",
        json_schema_extra={"x-effective": True, "since_milestone": since},
        **kwargs,
    )


def reserved(description: str, *, since: str = "TBD", **kwargs: Any) -> Any:
    """未生效字段：形状已就位、平台尚未消费（预留/后续里程碑接线）。"""
    return Field(
        description=f"[未生效] {description}",
        json_schema_extra={"x-effective": False, "since_milestone": since},
        **kwargs,
    )
