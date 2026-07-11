"""公共配置写操作 API（通知机器人 / LLM 模型登记）。

凭证类字段（机器人 config、模型 API key）一律 KEK 信封加密入库（红线9：明文不落库）。
JSON 端点，经 RBAC 写权限门控；会话 cookie SameSite=Lax 防跨站。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from payipa.ai.registry import get_system_prompt, register_model, set_default_model, set_model_enabled
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings as get_db_settings
from payipa.deliver.notify import NotifyBotStore
from pydantic import BaseModel, Field

from pyp_server.auth import require_perm

router = APIRouter(prefix="/api/config", tags=["config-manage"])


def _kek() -> str:
    return get_db_settings().cred_kek


# ── 通知机器人 ───────────────────────────────────────────────────────────────
class NotifyBotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    type: Literal["lark", "webhook", "email"] = Field(..., description="通知渠道类型")
    config: dict[str, Any] = Field(
        default_factory=dict, description="渠道配置（webhook url / lark token / smtp 等），KEK 加密存储"
    )


@router.post(
    "/notify-bots", summary="登记通知机器人（config KEK 加密）", dependencies=[Depends(require_perm("config.manage"))]
)
async def create_notify_bot(body: NotifyBotCreate) -> dict:
    bid = await NotifyBotStore(get_engine("pyp")).create(name=body.name, type=body.type, config=body.config, kek=_kek())
    return {"id": bid}


@router.delete("/notify-bots/{bot_id}", summary="删除通知机器人", dependencies=[Depends(require_perm("config.manage"))])
async def delete_notify_bot(bot_id: int) -> dict:
    if not await NotifyBotStore(get_engine("pyp")).delete(bot_id):
        raise HTTPException(status_code=404, detail=f"通知机器人 id={bot_id} 不存在")
    return {"id": bot_id, "deleted": True}


# ── LLM 模型 ─────────────────────────────────────────────────────────────────
class LlmModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="模型登记名（唯一，按 name upsert）")
    provider: str = Field(..., min_length=1, max_length=64, description="提供方，如 anthropic / echo")
    config: dict[str, Any] = Field(
        default_factory=dict, description="凭证与参数（api_key / model_id 等），KEK 加密存储"
    )
    enabled: bool = True
    make_default: bool = False


@router.post(
    "/llm-models", summary="登记/更新 LLM 模型（config KEK 加密）", dependencies=[Depends(require_perm("llm.manage"))]
)
async def create_llm_model(body: LlmModelCreate) -> dict:
    mid = await register_model(
        get_engine("pyp"), name=body.name, provider=body.provider, config=body.config,
        enabled=body.enabled, make_default=body.make_default, kek=_kek(),
    )  # fmt: skip
    return {"id": mid}


class EnabledRequest(BaseModel):
    enabled: bool


@router.post(
    "/llm-models/{model_id}/enabled", summary="启用 / 禁用模型", dependencies=[Depends(require_perm("llm.manage"))]
)
async def set_llm_enabled(model_id: int, body: EnabledRequest) -> dict:
    if not await set_model_enabled(get_engine("pyp"), model_id, body.enabled):
        raise HTTPException(status_code=404, detail=f"模型 id={model_id} 不存在")
    return {"id": model_id, "enabled": body.enabled}


@router.post(
    "/llm-models/{model_id}/default", summary="设为平台默认模型", dependencies=[Depends(require_perm("llm.manage"))]
)
async def set_llm_default(model_id: int) -> dict:
    if not await set_default_model(get_engine("pyp"), model_id):
        raise HTTPException(status_code=404, detail=f"模型 id={model_id} 不存在")
    return {"id": model_id, "is_default": True}


# ── 系统提示词（读单条全文；写走既有 /api/llm/prompts，更新即版本 +1）─────────────
@router.get(
    "/system-prompts/{name}",
    summary="取系统提示词全文（供界面编辑加载）",
    dependencies=[Depends(require_perm("llm.manage"))],
)
async def get_prompt(name: str) -> dict:
    content = await get_system_prompt(get_engine("pyp"), name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"系统提示词 {name!r} 不存在")
    return {"name": name, "content": content}
