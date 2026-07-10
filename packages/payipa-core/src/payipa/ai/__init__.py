"""ai —— AI 能力（08）：ModelRegistry（管理员配模型/凭证信封）+ LlmGateway（运行期路由/成本溯源）
+ provider 抽象（首个一等实现 = Anthropic 官方 SDK）。

沙箱脚本 `ctx.ai()`、「AI 帮写规则/组装」、界面一键生成都经 `gateway.complete` 单一出网入口调 AI：
按 registry 路由模型、KEK 解密凭证（脚本不接触明文）、每次调用挂 task_id/owner 记审计（成本溯源）。
生成脚本与人写脚本同轨（test 试跑 + 签名发布，见 studio/deliver 签名门）；本模块只管「调模型拿文本」。
"""

from __future__ import annotations

from payipa.ai import gateway, registry
from payipa.ai.provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    LLMProvider,
    LLMResult,
    build_provider,
)
from payipa.ai.registry import (
    ModelHandle,
    default_model_id,
    get_system_prompt,
    list_models,
    register_model,
    resolve_model,
    set_system_prompt,
)

__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "AnthropicProvider",
    "LLMProvider",
    "LLMResult",
    "ModelHandle",
    "build_provider",
    "default_model_id",
    "gateway",
    "get_system_prompt",
    "list_models",
    "register_model",
    "registry",
    "resolve_model",
    "set_system_prompt",
]
