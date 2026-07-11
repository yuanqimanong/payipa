"""ModelRegistry（08 §定案-1 + §4.5）：管理员配置模型清单，凭证信封加密存储，默认模型可标注。

`llm_models` 表：name / provider / config(JSONB 密文) / enabled。config 明文形如
``{api_key, base_url?, model_id?, max_tokens?, pricing?}``，经 KEK 信封加密（红线9，脚本不接触明文）。
默认模型 id 存 `global_params` 的 ``default_llm_model_id`` 键（ctx.ai 缺省选它）。
系统提示词在 `system_prompts` 表（项目默认提供、管理员可改）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import GlobalParam, LlmModel, SystemPrompt
from payipa.security.secrets import decrypt_json, encrypt_json

_DEFAULT_KEY = "default_llm_model_id"


@dataclass(slots=True)
class ModelHandle:
    """解析出的模型句柄：provider + 解密后的 config + 有效 model_id（供 gateway 直接调 provider）。"""

    id: int
    name: str
    provider: str
    config: dict[str, Any]  # 已解密明文（仅主控可信侧持有）
    model_id: str


async def register_model(
    engine_pyp: AsyncEngine,
    *,
    name: str,
    provider: str,
    config: dict[str, Any],
    enabled: bool = True,
    make_default: bool = False,
    kek: str | None = None,
) -> int:
    """登记/更新一个模型（按 name upsert）。config 明文经 KEK 信封加密入库。返回 model id。"""
    enc = encrypt_json(config, kek=kek)
    async with engine_pyp.begin() as conn:
        existing = (await conn.execute(select(LlmModel.id).where(LlmModel.name == name))).scalar()
        if existing is None:
            model_id = (
                await conn.execute(
                    pg_insert(LlmModel.__table__)
                    .values(name=name, provider=provider, config={"enc": enc}, enabled=enabled)
                    .returning(LlmModel.id)
                )
            ).scalar_one()
        else:
            model_id = int(existing)
            await conn.execute(
                update(LlmModel.__table__)
                .where(LlmModel.id == model_id)
                .values(provider=provider, config={"enc": enc}, enabled=enabled)
            )
        if make_default:
            await conn.execute(
                pg_insert(GlobalParam.__table__)
                .values(key=_DEFAULT_KEY, value={"model_id": model_id})
                .on_conflict_do_update(
                    index_elements=["key"], set_={"value": {"model_id": model_id}, "updated_at": func.now()}
                )
            )
    return model_id


async def set_model_enabled(engine_pyp: AsyncEngine, model_id: int, enabled: bool) -> bool:
    """启用/禁用模型；返回是否命中该模型。"""
    async with engine_pyp.begin() as conn:
        result = await conn.execute(update(LlmModel.__table__).where(LlmModel.id == model_id).values(enabled=enabled))
    return bool(result.rowcount)


async def set_default_model(engine_pyp: AsyncEngine, model_id: int) -> bool:
    """把某模型设为平台默认（写 global_params）；模型不存在返回 False。"""
    async with engine_pyp.begin() as conn:
        if not (await conn.execute(select(LlmModel.id).where(LlmModel.id == model_id))).first():
            return False
        await conn.execute(
            pg_insert(GlobalParam.__table__)
            .values(key=_DEFAULT_KEY, value={"model_id": model_id})
            .on_conflict_do_update(
                index_elements=["key"], set_={"value": {"model_id": model_id}, "updated_at": func.now()}
            )
        )
    return True


async def list_models(engine_pyp: AsyncEngine) -> list[dict[str, Any]]:
    """列出模型元信息（不含凭证明文，安全回显给界面）。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(LlmModel.id, LlmModel.name, LlmModel.provider, LlmModel.enabled).order_by(LlmModel.id)
            )
        ).all()
        default_id = (await conn.execute(select(GlobalParam.value).where(GlobalParam.key == _DEFAULT_KEY))).scalar()
    default_model_id = (default_id or {}).get("model_id") if isinstance(default_id, dict) else None
    return [
        {"id": i, "name": n, "provider": p, "enabled": bool(e), "is_default": i == default_model_id}
        for i, n, p, e in rows
    ]


async def default_model_id(engine_pyp: AsyncEngine) -> int | None:
    """平台默认模型 id（未设返回 None）。"""
    async with engine_pyp.connect() as conn:
        v = (await conn.execute(select(GlobalParam.value).where(GlobalParam.key == _DEFAULT_KEY))).scalar()
    return v.get("model_id") if isinstance(v, dict) else None


async def resolve_model(
    engine_pyp: AsyncEngine, *, name: str | None = None, model_pk: int | None = None, kek: str | None = None
) -> ModelHandle:
    """解析模型句柄：按 name / 主键指定，或都不给取平台默认。解密 config、校验 enabled。

    优先级：model_pk > name > 默认。未找到/被禁用/未配默认 → ValueError（面向 gateway）。
    """
    async with engine_pyp.connect() as conn:
        if model_pk is None and name is None:
            model_pk = await default_model_id(engine_pyp)
            if model_pk is None:
                raise ValueError("未配置平台默认模型（管理员先 register_model(make_default=True)）")
        stmt = select(LlmModel.id, LlmModel.name, LlmModel.provider, LlmModel.config, LlmModel.enabled)
        stmt = stmt.where(LlmModel.id == model_pk) if model_pk is not None else stmt.where(LlmModel.name == name)
        row = (await conn.execute(stmt)).first()
    if row is None:
        raise ValueError(f"模型不存在（name={name!r} id={model_pk!r}）")
    mid, mname, provider, config_col, enabled = row
    if not enabled:
        raise ValueError(f"模型 '{mname}' 已禁用")
    enc = (config_col or {}).get("enc")
    if not enc:
        raise ValueError(f"模型 '{mname}' 未配置凭证")
    config = decrypt_json(enc, kek=kek)
    from payipa.ai.provider import DEFAULT_ANTHROPIC_MODEL

    resolved_model_id = str(config.get("model_id") or DEFAULT_ANTHROPIC_MODEL)
    return ModelHandle(id=int(mid), name=mname, provider=provider, config=config, model_id=resolved_model_id)


async def set_system_prompt(engine_pyp: AsyncEngine, *, name: str, content: str) -> int:
    """存/更新一个系统提示词（按 name；更新则版本 +1）。返回 prompt id。"""
    async with engine_pyp.begin() as conn:
        existing = (
            await conn.execute(select(SystemPrompt.id, SystemPrompt.version).where(SystemPrompt.name == name))
        ).first()
        if existing is None:
            return int(
                (
                    await conn.execute(
                        pg_insert(SystemPrompt.__table__)
                        .values(name=name, content=content, version=1)
                        .returning(SystemPrompt.id)
                    )
                ).scalar_one()
            )
        pid, ver = existing
        await conn.execute(
            update(SystemPrompt.__table__).where(SystemPrompt.id == pid).values(content=content, version=int(ver) + 1)
        )
        return int(pid)


async def get_system_prompt(engine_pyp: AsyncEngine, name: str) -> str | None:
    """取系统提示词内容（最新版本）；不存在返回 None。"""
    async with engine_pyp.connect() as conn:
        return (await conn.execute(select(SystemPrompt.content).where(SystemPrompt.name == name))).scalar()


async def list_system_prompts(engine_pyp: AsyncEngine) -> list[dict[str, Any]]:
    """列出系统提示词元信息（name/version/内容长度；不回全文，界面按需 GET 单条）。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(SystemPrompt.id, SystemPrompt.name, SystemPrompt.version, SystemPrompt.content).order_by(
                    SystemPrompt.name
                )
            )
        ).all()
    return [{"id": r.id, "name": r.name, "version": r.version, "length": len(r.content or "")} for r in rows]


__all__ = [
    "ModelHandle",
    "default_model_id",
    "get_system_prompt",
    "list_models",
    "list_system_prompts",
    "register_model",
    "resolve_model",
    "set_default_model",
    "set_model_enabled",
    "set_system_prompt",
]
