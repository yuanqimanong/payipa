"""规则获取：任务带 (rule_id, version, content_hash) 指针 → 本地内容寻址缓存 → 未命中拉取验 hash。

M0 骨架：缓存按 hash 不可变（内容寻址红利，无需失效通知）。实现于 M1。
"""

from __future__ import annotations

from payipa_contracts import RulePack, RulePointer


class RuleCache:
    """agent 本地内容寻址缓存（按 content_hash 不可变）。"""

    async def get(self, ptr: RulePointer) -> RulePack:
        """命中缓存直接返回；未命中向主控 /internal/rules/{hash} 拉取并验 hash 后落缓存。"""
        raise NotImplementedError("M1：内容寻址缓存 + 未命中拉取校验 hash")
