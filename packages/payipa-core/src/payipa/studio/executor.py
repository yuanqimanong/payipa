"""组装脚本执行器（CodeExecutor）+ 执行上下文（AssembleContext）。

组装脚本 = 用户 Python（固定方法名 `assemble(ctx)`），经内容寻址 + 签名，由执行器跑（生产默认 Sandbox、
Local 仅降级）。脚本**只拿 ctx.read_table 取数**，从不拿 DB 引擎（红线2）。M3 首刀提供 LocalExecutor（进程内、
管理员签名脚本，防御深度可接受）坐实主链；真 SandboxExecutor（专用出网 + sidecar 上传）在后续切片替换实现。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from payipa_contracts import ColumnFilter, TableQueryRequest
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.studio.gateway import QueryGateway

# 组装脚本签名：拿上下文、产出「产物字段行」列表
AssembleFn = Callable[["AssembleContext"], Awaitable[list[dict]]]


class AssembleContext:
    """交给组装脚本的唯一取数入口。脚本经 ctx.read_table 读源数据（走 Query Gateway），不接触 DB。"""

    def __init__(self, engine_dc: AsyncEngine, gateway: QueryGateway | None = None) -> None:
        self._dc = engine_dc
        self._gw = gateway or QueryGateway()

    async def read_table(
        self,
        source: str,
        *,
        columns: list[str] | None = None,
        filters: list[ColumnFilter] | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """读某数据源全部（自动翻页）行；返回投影后的行 dict 列表。经 Query Gateway，无 SQL、无 DB 句柄。"""
        rows: list[dict] = []
        cursor = None
        while True:
            req = TableQueryRequest(source=source, columns=columns, filters=filters or [], limit=limit, cursor=cursor)
            page, cursor, _ = await self._gw.read(self._dc, req)
            rows.extend(page)
            if cursor is None:
                return rows


class CodeExecutor(Protocol):
    """执行器契约：给定组装脚本 + 上下文，产出产物字段行。实现有 LocalExecutor（降级）/ SandboxExecutor（默认）。"""

    async def run(self, script: AssembleFn, ctx: AssembleContext) -> list[dict]: ...


class LocalExecutor:
    """进程内执行（降级路径）：直接 await 脚本。仅用于管理员签名脚本 / 无沙箱的开发环境。"""

    async def run(self, script: AssembleFn, ctx: AssembleContext) -> list[dict]:
        return await script(ctx)
