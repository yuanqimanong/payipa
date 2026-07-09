"""Query Gateway 契约（M3 组装取数）：结构化表查询请求形状——**无 SQL 串**，用户/AI 代码经此读数（红线2）。

只描述传输形状（零 I/O）；Arrow IPC 是传输编码、不进 contracts。取数/鉴权/配额逻辑在 core.studio，
apps/server 仅装配 `/internal/query` 端点。M3 首刀为进程内网关；HTTP+Arrow 边界在后续切片。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from payipa_contracts._annotate import active
from payipa_contracts.enums import FilterOp


class ColumnFilter(BaseModel):
    """单列过滤条件（AND 组合）。column 为系统列（id/state/created_at…）或用户字段名（走 fields ->> 名）。"""

    column: str = active("列名：系统列或用户字段名", since="M3")
    op: FilterOp = active("算子", since="M3")
    value: Any = active("比较值（IN 为列表）", default=None, since="M3")


class KeysetCursor(BaseModel):
    """键集游标（按 id 升序翻页）。M3 首刀仅 after_id；签名不可伪造的 opaque 游标留后续切片。"""

    after_id: int = active("上一页最后一行 id；取 id > after_id", default=0, ge=0, since="M3")


class TableQueryRequest(BaseModel):
    """一次结构化表查询（读某数据源的 data_* 行）。过滤为 AND、单列 id 升序 + keyset（M3 首刀）。"""

    source: str = active("数据源短码（读其 data_{source} 表）", since="M3")
    columns: list[str] | None = active("投影列（None=返回系统列 + fields 袋）", default=None, since="M3")
    filters: list[ColumnFilter] = active("过滤条件（AND）", default_factory=list, since="M3")
    limit: int = active("单页最大行数", default=500, gt=0, le=10000, since="M3")
    cursor: KeysetCursor | None = active("键集游标（进程内直调用）", default=None, since="M3")
    cursor_token: str | None = active(
        "签名不透明游标（HTTP 网关翻页用；由上一页响应下发，沙箱不可伪造/跨作业重用）", default=None, since="M3"
    )


class QuotaMeta(BaseModel):
    """配额/用量回执（喂 job_token 行数配额，资源限额第 2 层）。M3 首刀仅统计本次返回行数。"""

    rows_returned: int = active("本次返回行数", ge=0, since="M3")
    quota: int | None = active("行数配额上限（None=未限）", default=None, since="M3")
    rows_remaining: int | None = active("剩余配额（None=未限/未知）", default=None, since="M3")
