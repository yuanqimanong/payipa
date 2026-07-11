"""公共配置写操作集成测试（需 PG）：通知机器人建/删 + LLM 模型登记/启停/设默认。凭证 KEK 加密不落明文。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import LlmModel, NotifyBot, SystemPrompt
from payipa.db.settings import get_settings
from pyp_server.main import create_app
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

_BOT = "cfg-test-bot"
_MODEL = "cfg-test-model"
_PROMPT = "cfg-test-prompt"


async def _cleanup() -> None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.begin() as conn:
        await conn.execute(delete(NotifyBot.__table__).where(NotifyBot.name == _BOT))
        await conn.execute(delete(LlmModel.__table__).where(LlmModel.name == _MODEL))
        await conn.execute(delete(SystemPrompt.__table__).where(SystemPrompt.name == _PROMPT))
    await engine.dispose()


async def _bot_config_raw(name: str) -> dict | None:
    engine = create_async_engine(get_settings().async_url("pyp"))
    async with engine.connect() as conn:
        row = (await conn.execute(select(NotifyBot.config).where(NotifyBot.name == name))).scalar()
    await engine.dispose()
    return row


async def _config() -> dict:
    from payipa.views import config_overview

    engine = create_async_engine(get_settings().async_url("pyp"))
    try:
        return await config_overview(engine)
    finally:
        await engine.dispose()


def test_notify_bot_create_and_delete(require_pg: None) -> None:
    asyncio.run(_cleanup())
    try:
        with TestClient(create_app()) as client:
            r = client.post(
                "/api/config/notify-bots",
                json={
                    "name": _BOT,
                    "type": "webhook",
                    "config": {"url": "https://example.test/hook", "secret": "s3cr"},
                },
            )
            assert r.status_code == 200, r.text
            bid = r.json()["id"]

            # config 密文入库：不含明文 url/secret（KEK 信封加密）
            blob = str(asyncio.run(_bot_config_raw(_BOT)))
            assert "example.test" not in blob and "s3cr" not in blob

            # 存在于配置总览（core 视图，不经登录门控端点）
            assert any(b["id"] == bid and b["type"] == "webhook" for b in asyncio.run(_config()).get("notify_bots", []))

            # 删除 → 404 再删
            assert client.delete(f"/api/config/notify-bots/{bid}").status_code == 200
            assert client.delete(f"/api/config/notify-bots/{bid}").status_code == 404
            # 非法类型 → 422
            assert (
                client.post("/api/config/notify-bots", json={"name": "x", "type": "sms", "config": {}}).status_code
                == 422
            )
    finally:
        asyncio.run(_cleanup())


def test_llm_model_register_enable_default(require_pg: None) -> None:
    asyncio.run(_cleanup())
    try:
        with TestClient(create_app()) as client:
            # 登记 echo 模型（离线，无真 key）
            r = client.post(
                "/api/config/llm-models",
                json={"name": _MODEL, "provider": "echo", "config": {"note": "smoke"}, "make_default": True},
            )
            assert r.status_code == 200, r.text
            mid = r.json()["id"]

            models = {m["id"]: m for m in asyncio.run(_config())["models"]}
            assert models[mid]["provider"] == "echo" and models[mid]["enabled"] is True
            assert models[mid]["is_default"] is True

            # 禁用 → enabled False
            assert client.post(f"/api/config/llm-models/{mid}/enabled", json={"enabled": False}).status_code == 200
            assert next(m for m in asyncio.run(_config())["models"] if m["id"] == mid)["enabled"] is False

            # 重新设默认（幂等）+ 不存在模型 404
            assert client.post(f"/api/config/llm-models/{mid}/default").status_code == 200
            assert client.post("/api/config/llm-models/99999999/default").status_code == 404
            assert client.post("/api/config/llm-models/99999999/enabled", json={"enabled": True}).status_code == 404
    finally:
        asyncio.run(_cleanup())


def test_system_prompt_edit(require_pg: None) -> None:
    """系统提示词：存（走既有 /api/llm/prompts）→ GET 全文回来 → 再存版本 +1 → 出现在总览。"""
    asyncio.run(_cleanup())
    try:
        with TestClient(create_app()) as client:
            assert (
                client.post("/api/llm/prompts", json={"name": _PROMPT, "content": "你是数据抽取助手。"}).status_code
                == 200
            )
            got = client.get(f"/api/config/system-prompts/{_PROMPT}")
            assert got.status_code == 200 and got.json()["content"] == "你是数据抽取助手。"

            # 同名再存 → 版本 +1
            assert client.post("/api/llm/prompts", json={"name": _PROMPT, "content": "改一版。"}).status_code == 200
            prompts = {p["name"]: p for p in asyncio.run(_config())["system_prompts"]}
            assert prompts[_PROMPT]["version"] == 2
            assert client.get(f"/api/config/system-prompts/{_PROMPT}").json()["content"] == "改一版。"

            # 不存在 → 404
            assert client.get("/api/config/system-prompts/no-such-prompt").status_code == 404
    finally:
        asyncio.run(_cleanup())
