"""AI 能力 REST API（08 §定案 + §4.5）：模型清单管理 + 系统提示词 + 一次补全（经 LlmGateway）。

均经 `llm.manage` 权限闸门（M5 RBAC）。凭证信封加密存储（KEK 留平台侧，回显不含明文）。
运行期 `ctx.ai()` 直接走 core `ai.gateway.complete`（沙箱内），本路由是管理员配置 + 界面「一键生成」入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from payipa.ai import gateway, registry
from payipa.db.engine import get_engine
from pydantic import BaseModel, Field

from pyp_server.auth import get_current_user, require_perm

router = APIRouter(prefix="/api/llm", tags=["ai"])


class ModelUpsertRequest(BaseModel):
    name: str = Field(..., description="模型清单里的展示名（按 name upsert）")
    provider: str = Field("anthropic", description="provider 名（当前一等：anthropic）")
    config: dict[str, Any] = Field(
        ..., description="明文凭证/参数 {api_key, base_url?, model_id?, ...}；入库前 KEK 加密"
    )
    enabled: bool = Field(True, description="是否启用")
    make_default: bool = Field(False, description="设为平台默认模型（ctx.ai 缺省选它）")


class SystemPromptRequest(BaseModel):
    name: str = Field(..., description="提示词名（按 name；更新则版本 +1）")
    content: str = Field(..., description="系统提示词内容")


class CompleteRequest(BaseModel):
    prompt: str = Field(..., description="用户提示")
    model_name: str | None = Field(None, description="指定模型名（不填取平台默认）")
    system: str | None = Field(None, description="系统提示词（不填则不带）")
    system_prompt_name: str | None = Field(None, description="取 registry 里某系统提示词（与 system 二选一）")
    task_id: str | None = Field(None, description="成本溯源：挂到该任务")
    max_tokens: int | None = Field(None, ge=1, description="覆盖 max_tokens")


class CompleteResponse(BaseModel):
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


@router.post(
    "/models", summary="登记/更新一个模型（凭证 KEK 加密存储）", dependencies=[Depends(require_perm("llm.manage"))]
)
async def upsert_model(body: ModelUpsertRequest) -> dict:
    model_id = await registry.register_model(
        get_engine("pyp"),
        name=body.name,
        provider=body.provider,
        config=body.config,
        enabled=body.enabled,
        make_default=body.make_default,
    )
    return {"id": model_id}


@router.get("/models", summary="模型清单（不含凭证明文）", dependencies=[Depends(require_perm("llm.manage"))])
async def list_models() -> list[dict]:
    return await registry.list_models(get_engine("pyp"))


@router.post("/prompts", summary="设置系统提示词（更新则版本 +1）", dependencies=[Depends(require_perm("llm.manage"))])
async def set_prompt(body: SystemPromptRequest) -> dict:
    pid = await registry.set_system_prompt(get_engine("pyp"), name=body.name, content=body.content)
    return {"id": pid}


@router.post(
    "/complete",
    response_model=CompleteResponse,
    summary="一次 LLM 补全（经 Gateway 路由 + 成本审计）",
    dependencies=[Depends(require_perm("llm.manage"))],
)
async def complete(body: CompleteRequest, request: Request) -> CompleteResponse:
    pyp = get_engine("pyp")
    user = await get_current_user(request)
    system = body.system
    if system is None and body.system_prompt_name:
        system = await registry.get_system_prompt(pyp, body.system_prompt_name)
    params = {"max_tokens": body.max_tokens} if body.max_tokens else {}
    try:
        result = await gateway.complete(
            pyp,
            body.prompt,
            model_name=body.model_name,
            system=system,
            params=params,
            task_id=body.task_id,
            owner=int(user["id"]) if user else None,
        )
    except ValueError as exc:  # 模型未配/禁用/无默认
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— provider/网络失败回 502（已记审计）
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{exc}") from exc
    return CompleteResponse(
        text=result.text,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
