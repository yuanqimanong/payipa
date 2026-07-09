"""组装脚本执行器（CodeExecutor）+ 执行上下文（AssembleContext）。

组装脚本 = 用户 Python（固定方法名 `assemble(ctx)`），经内容寻址 + 签名，由执行器跑（生产默认 Sandbox、
Local 仅降级）。脚本**只拿 ctx.read_table 取数**，从不拿 DB 引擎（红线2）。M3 首刀提供 LocalExecutor（进程内、
管理员签名脚本，防御深度可接受）坐实主链；真 SandboxExecutor（专用出网 + sidecar 上传）在后续切片替换实现。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from payipa_contracts import ColumnFilter, KeysetCursor, TableQueryRequest
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.studio.gateway import QueryGateway

# 组装脚本签名：拿上下文、产出「产物字段行」列表
AssembleFn = Callable[["AssembleContext"], Awaitable[list[dict]]]


class AssembleContext:
    """交给组装脚本的唯一取数入口。脚本经 ctx.read_table 读源数据（走 Query Gateway），不接触 DB。

    增量组装（M3 slice-8）：构造时传 watermarks={源: 已消费到的 id}，脚本 read_table(incremental=True) 只读
    id 大于水位的新增行；读到的每源最大 id 记入 new_watermarks，供 run 层组装成功后推进水位（写腿指纹幂等）。
    """

    def __init__(
        self,
        engine_dc: AsyncEngine,
        gateway: QueryGateway | None = None,
        *,
        watermarks: dict[str, int] | None = None,
    ) -> None:
        self._dc = engine_dc
        self._gw = gateway or QueryGateway()
        self._start_wm = dict(watermarks or {})
        self.new_watermarks: dict[str, int] = {}

    async def read_table(
        self,
        source: str,
        *,
        columns: list[str] | None = None,
        filters: list[ColumnFilter] | None = None,
        limit: int = 500,
        incremental: bool = False,
    ) -> list[dict]:
        """读某数据源全部（自动翻页）行；返回投影后的行 dict 列表。经 Query Gateway，无 SQL、无 DB 句柄。

        incremental=True：从该源水位（默认 0）之后读起，并把读到的最大 id 记入 new_watermarks[source]；
        若脚本未在 columns 中要 id，临时借 id 追踪水位、返回前剥掉，不污染脚本可见字段。
        """
        after = self._start_wm.get(source, 0) if incremental else 0
        fetch_cols = columns
        strip_id = False
        if incremental and columns is not None and "id" not in columns:
            fetch_cols = [*columns, "id"]  # 借 id 追踪水位
            strip_id = True
        rows: list[dict] = []
        cursor: KeysetCursor | None = KeysetCursor(after_id=after) if after else None
        max_id = after
        while True:
            req = TableQueryRequest(
                source=source, columns=fetch_cols, filters=filters or [], limit=limit, cursor=cursor
            )
            page, cursor, _ = await self._gw.read(self._dc, req)
            for row in page:
                rid = row.get("id")
                if isinstance(rid, int) and rid > max_id:
                    max_id = rid
            rows.extend(page)
            if cursor is None:
                break
        if incremental:
            self.new_watermarks[source] = max_id
            if strip_id:
                for row in rows:
                    row.pop("id", None)
        return rows


class CodeExecutor(Protocol):
    """执行器契约：给定组装脚本 + 上下文，产出产物字段行。实现有 LocalExecutor（降级）/ SandboxExecutor（默认）。"""

    async def run(self, script: AssembleFn, ctx: AssembleContext) -> list[dict]: ...


class LocalExecutor:
    """进程内执行（降级路径）：直接 await 脚本。仅用于管理员签名脚本 / 无沙箱的开发环境。"""

    async def run(self, script: AssembleFn, ctx: AssembleContext) -> list[dict]:
        return await script(ctx)
