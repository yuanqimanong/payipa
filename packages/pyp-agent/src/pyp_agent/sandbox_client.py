"""解析/清洗脚本执行：发本机 sidecar 沙箱（无则本地执行降级）。按批不按条。

M0 骨架：声明式 JSON 规则由内置解释器本地跑（不进沙箱）；仅 Python 脚本进 sidecar。实现于 M1/M3。
"""

from __future__ import annotations

from payipa_contracts import Item


class SandboxClient:
    """代码执行器客户端：本机 sidecar（localhost，数据不出机）或本地执行降级。"""

    async def run_batch(self, req_id: str, raw_payloads: list[bytes]) -> list[Item]:
        """按批把原始响应交给执行器解析+清洗，取回 Item（含 FieldMeta）。"""
        raise NotImplementedError("M1/M3：按批不按条调用 sidecar / 本地执行器")
