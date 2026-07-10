"""LlmGateway（08 §4.5 运行期口径）：按 ModelRegistry 路由 → 解密凭证 → provider 补全 → 成本/审计溯源。

沙箱脚本 `ctx.ai()`、「AI 帮写规则/组装」、界面「一键生成」都经此单一入口出网调 AI：
- **模型路由**：按 name / 主键选 registry 里的模型，或缺省取平台默认；Gateway 不自行接管凭证与选型。
- **成本/审计**：每次调用挂 `task_id`/`owner` 记 `pyp.audit_log`（action=``llm.call``；token/成本/模型）。
  Langfuse 自部署接入留后续（本切片先落库审计，口径与 04A「审计与成本记录」一致）。
- **凭证**：信封加密，KEK 留平台侧、脚本不接触明文（resolve_model 内解密，仅主控可信侧）。

provider 可注入（测试用 stub，无需真 key / 真网络）；缺省按 registry 里的 provider 名构建。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.ai.provider import LLMProvider, LLMResult, build_provider
from payipa.ai.registry import resolve_model
from payipa.security.audit import record_audit_best_effort


async def complete(
    engine_pyp: AsyncEngine,
    prompt: str,
    *,
    model_name: str | None = None,
    model_pk: int | None = None,
    system: str | None = None,
    params: dict[str, Any] | None = None,
    task_id: str | None = None,
    owner: int | None = None,
    kek: str | None = None,
    provider: LLMProvider | None = None,
) -> LLMResult:
    """一次 LLM 补全（运行期唯一入口）。

    解析模型（name / 主键 / 默认）→ 解密凭证 → 调 provider → 记审计（成本挂 task_id/owner）。
    provider 显式传入则跳过 registry 构建（测试注入 stub）。失败照样记一条审计再抛。
    """
    handle = await resolve_model(engine_pyp, name=model_name, model_pk=model_pk, kek=kek)
    prov = provider if provider is not None else build_provider(handle.provider, handle.config)
    result: LLMResult | None = None
    error: str | None = None
    try:
        result = await prov.complete(prompt=prompt, system=system, model_id=handle.model_id, params=params or {})
        return result
    except Exception as exc:  # noqa: BLE001 —— 记审计后原样抛给调用方
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await record_audit_best_effort(
            engine_pyp,
            action="llm.call",
            actor_id=owner,
            object_type="llm_model",
            object_id=str(handle.id),
            after={
                "model": handle.model_id,
                "provider": handle.provider,
                "task_id": task_id,
                "input_tokens": result.input_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "cost_usd": result.cost_usd if result else None,
                "ok": error is None,
                "error": error,
            },
            source="llm_gateway",
        )


__all__ = ["complete"]
