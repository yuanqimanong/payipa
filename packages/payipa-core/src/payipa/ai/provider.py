"""LLM provider 抽象（08 §4.5）：统一 complete 接口 + 首个一等 provider = Anthropic 官方 SDK。

沙箱脚本 `ctx.ai()` 与「AI 帮写规则/组装」都经 LlmGateway 路由到某个 provider。provider 只做
「一次补全」——凭证/选型/成本审计由 gateway 负责（provider 不接管凭证解密与 registry）。
具体接入协议（OpenAI 兼容 / 本地推理）留实现期扩展；本切片落 Anthropic 官方 SDK 一等实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# 默认模型（Anthropic 最新最强通用模型）；管理员未在 config 指定 model_id 时用它。
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# 每百万 token 价格（美元；用于成本溯源，可被 config.pricing 覆盖）。来源：模型价目表缓存。
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}


@dataclass(slots=True)
class LLMResult:
    """一次补全结果：文本 + token 用量 + 解析出的成本（USD，无价目表则 None）。"""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """provider 契约：给定 prompt/system/参数，返回一次补全。同步或异步实现均可（gateway 用 anyio 适配）。"""

    async def complete(
        self, *, prompt: str, system: str | None, model_id: str, params: dict[str, Any]
    ) -> LLMResult: ...


def _price(model_id: str, input_tokens: int, output_tokens: int, pricing: dict | None) -> float | None:
    """按每百万 token 价目算成本；无价目返回 None（诚实：未知成本不瞎报）。"""
    rate = None
    if pricing and model_id in pricing:
        rate = tuple(pricing[model_id])
    elif model_id in _ANTHROPIC_PRICING:
        rate = _ANTHROPIC_PRICING[model_id]
    if rate is None:
        return None
    in_rate, out_rate = rate
    return round(input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate, 6)


class AnthropicProvider:
    """Anthropic 官方 SDK provider（`anthropic` 包，惰性导入——无 key/无包时不阻断 import）。

    config 约定键：``api_key``（必需）、``base_url``（可选）、``model_id``（缺省 opus-4-8）、
    ``max_tokens``（缺省 4096）、``pricing``（可选，覆盖内置价目）。凭证由 gateway 解密后传入。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    async def complete(self, *, prompt: str, system: str | None, model_id: str, params: dict[str, Any]) -> LLMResult:
        import anthropic  # 惰性导入：仅真正调用时才需要 SDK 与网络

        client = anthropic.AsyncAnthropic(
            api_key=self._config["api_key"],
            base_url=self._config.get("base_url") or None,
        )
        max_tokens = int(params.get("max_tokens") or self._config.get("max_tokens") or 4096)
        kwargs: dict[str, Any] = {"model": model_id, "max_tokens": max_tokens}
        if system:
            kwargs["system"] = system
        kwargs["messages"] = [{"role": "user", "content": prompt}]
        try:
            resp = await client.messages.create(**kwargs)
        finally:
            await client.close()
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = resp.usage
        in_tok, out_tok = int(usage.input_tokens), int(usage.output_tokens)
        return LLMResult(
            text=text,
            model=resp.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=_price(resp.model, in_tok, out_tok, self._config.get("pricing")),
            raw={"stop_reason": resp.stop_reason},
        )


class EchoProvider:
    """离线/开发 provider（无网络、无 key）：把 prompt 回显为补全，成本恒 0。

    供本地起步冒烟与联调用（管理员配 provider=``echo`` 的模型即可验证整条 Gateway 链路不出网）；
    生产用 ``anthropic`` 等真 provider。token 用量按字符粗估（仅示意，不计费）。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    async def complete(self, *, prompt: str, system: str | None, model_id: str, params: dict[str, Any]) -> LLMResult:
        text = f"[echo:{model_id}] {prompt}"
        return LLMResult(
            text=text,
            model=model_id,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            cost_usd=0.0,
            raw={"provider": "echo"},
        )


# provider 名 → 工厂（config → provider 实例）。新 provider 在此登记（OpenAI 兼容/本地推理留实现期）。
PROVIDERS: dict[str, Callable[[dict[str, Any]], LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "echo": EchoProvider,  # 离线/开发（无 key、无网络）
}


def build_provider(provider: str, config: dict[str, Any]) -> LLMProvider:
    """按名建 provider 实例；未知 provider 抛 ValueError。"""
    factory = PROVIDERS.get(provider)
    if factory is None:
        raise ValueError(f"未知 LLM provider：{provider!r}（已登记：{sorted(PROVIDERS)}）")
    return factory(config)


__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "AnthropicProvider",
    "EchoProvider",
    "LLMProvider",
    "LLMResult",
    "PROVIDERS",
    "build_provider",
]
