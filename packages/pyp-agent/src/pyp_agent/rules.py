"""规则获取：任务带 (rule_id, version, content_hash) 指针 → 本地内容寻址缓存 → 未命中拉取。

缓存按 content_hash 不可变（内容寻址红利，无需失效通知）。M1：未命中向主控 /internal/rules/{hash} 拉取。
"""

from __future__ import annotations

import niquests
from payipa_contracts import RulePack, RulePointer


class RuleCache:
    """agent 本地内容寻址缓存（按 content_hash 不可变）。"""

    def __init__(self, server_base: str) -> None:
        self.server_base = server_base.rstrip("/")
        self._cache: dict[str, RulePack] = {}

    async def get(self, ptr: RulePointer) -> RulePack:
        cached = self._cache.get(ptr.content_hash)
        if cached is not None:
            return cached
        async with niquests.AsyncSession() as session:
            resp = await session.get(f"{self.server_base}/internal/rules/{ptr.content_hash}", timeout=30)
        resp.raise_for_status()
        pack = RulePack.model_validate(resp.json())
        self._cache[ptr.content_hash] = pack  # 内容寻址不可变 → 永久缓存
        return pack
