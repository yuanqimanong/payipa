"""M5 AI/LLM Gateway 集成测试（需 PG）：ModelRegistry 凭证信封 + 默认模型 + 系统提示词 +
Gateway 路由/成本审计（注入 stub provider，无需真 key）+ HTTP 端点 llm.manage 闸门（echo provider）。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.ai import gateway, registry
from payipa.ai.provider import LLMResult
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.security.rbac import assign_role, make_superuser, seed_default_rbac
from payipa.security.secrets import decrypt_json
from pyp_server.auth import COOKIE_NAME, create_session
from pyp_server.main import app
from pyp_server.settings import get_server_settings
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine


class _StubProvider:
    """测试用 provider：记录收到的凭证/参数，返回固定文本 + token 用量。"""

    def __init__(self) -> None:
        self.seen: dict = {}

    async def complete(self, *, prompt, system, model_id, params) -> LLMResult:
        self.seen = {"prompt": prompt, "system": system, "model_id": model_id, "params": params}
        return LLMResult(text=f"ok:{prompt}", model=model_id, input_tokens=10, output_tokens=5, cost_usd=0.001)


async def _purge(pyp) -> None:
    async with pyp.begin() as conn:
        await conn.execute(text("DELETE FROM llm_models WHERE name LIKE 'ai-test-%'"))
        await conn.execute(text("DELETE FROM system_prompts WHERE name LIKE 'ai-test-%'"))
        await conn.execute(text("DELETE FROM global_params WHERE key = 'default_llm_model_id'"))
        await conn.execute(
            text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'ai-%')")
        )
        await conn.execute(text("DELETE FROM users WHERE username LIKE 'ai-%'"))


def test_registry_and_gateway(require_pg: None) -> None:
    """凭证信封加密 + 默认模型解析 + Gateway 注入 stub 调用 + 审计落库（含成本）。"""

    async def main() -> dict:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            # 登记模型（凭证明文 → 入库应为密文）+ 设默认
            mid = await registry.register_model(
                pyp,
                name="ai-test-claude",
                provider="anthropic",
                config={"api_key": "sk-secret-xyz", "model_id": "claude-opus-4-8"},
                make_default=True,
            )
            # 库里 config 是密文（不含明文 key），解密还原
            async with pyp.connect() as conn:
                raw = (await conn.execute(text("SELECT config FROM llm_models WHERE id=:i"), {"i": mid})).scalar()
            assert "sk-secret-xyz" not in str(raw)  # 明文不落库
            assert decrypt_json(raw["enc"])["api_key"] == "sk-secret-xyz"  # 可解回

            # 默认模型解析
            assert await registry.default_model_id(pyp) == mid
            handle = await registry.resolve_model(pyp)  # 不指定 → 取默认
            assert handle.model_id == "claude-opus-4-8" and handle.config["api_key"] == "sk-secret-xyz"

            # 系统提示词存取 + 版本 +1
            await registry.set_system_prompt(pyp, name="ai-test-sp", content="v1")
            await registry.set_system_prompt(pyp, name="ai-test-sp", content="v2")
            assert await registry.get_system_prompt(pyp, "ai-test-sp") == "v2"

            # Gateway 注入 stub：路由到默认模型、解密凭证传给 provider、记审计
            stub = _StubProvider()
            before = await _audit_count(pyp)
            result = await gateway.complete(
                pyp, "写个爬虫", system="你是助手", task_id="batch-9", owner=1, provider=stub
            )
            after = await _audit_count(pyp)
            return {
                "result": result,
                "stub_seen": stub.seen,
                "audit_delta": after - before,
                "last_audit": await _last_audit(pyp),
            }
        finally:
            await _purge(pyp)
            await pyp.dispose()

    out = asyncio.run(main())
    r = out["result"]
    assert r.text == "ok:写个爬虫" and r.input_tokens == 10 and r.cost_usd == 0.001
    assert out["stub_seen"]["model_id"] == "claude-opus-4-8"  # gateway 传了解析出的 model_id
    assert out["stub_seen"]["system"] == "你是助手"
    assert out["audit_delta"] == 1  # 记了一条 llm.call
    a = out["last_audit"]
    assert a["action"] == "llm.call" and a["after"]["task_id"] == "batch-9"
    assert a["after"]["cost_usd"] == 0.001 and a["after"]["ok"] is True


async def _audit_count(pyp) -> int:
    async with pyp.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM audit_log WHERE action='llm.call'"))).scalar() or 0)


async def _last_audit(pyp) -> dict:
    async with pyp.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT action, after FROM audit_log WHERE action='llm.call' ORDER BY id DESC LIMIT 1")
            )
        ).first()
    return {"action": row[0], "after": row[1]}


def test_llm_endpoints_gated(require_pg: None) -> None:
    """HTTP：llm.manage 闸门 401/403/200 + echo provider 端到端补全（无需 key）。"""

    async def seed() -> dict[str, int]:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            await seed_default_rbac(pyp)
            ids: dict[str, int] = {}
            async with pyp.begin() as conn:
                for name in ("ai-super", "ai-op"):
                    uid = (
                        await conn.execute(
                            pg_insert(User.__table__)
                            .values(username=name, password_hash="x", status="active")
                            .returning(User.id)
                        )
                    ).scalar_one()
                    ids[name] = int(uid)
            await make_superuser(pyp, "ai-super")  # 通配 * → 有 llm.manage
            await assign_role(pyp, ids["ai-op"], "运营")  # 无 llm.manage
            return ids
        finally:
            await pyp.dispose()

    ids = asyncio.run(seed())
    settings = get_server_settings()
    settings.rbac_enabled = True
    try:
        with TestClient(app) as client:
            body = {"name": "ai-test-echo", "provider": "echo", "config": {}, "make_default": True}
            # 未登录 → 401
            assert client.post("/api/llm/models", json=body).status_code == 401
            # 运营（无 llm.manage）→ 403
            client.cookies.set(COOKIE_NAME, create_session(ids["ai-op"], "ai-op"))
            assert client.post("/api/llm/models", json=body).status_code == 403
            # 超级用户 → 登记 echo 模型 + 列表 + 补全（echo，无 key）
            client.cookies.set(COOKIE_NAME, create_session(ids["ai-super"], "ai-super"))
            assert client.post("/api/llm/models", json=body).status_code == 200
            models = client.get("/api/llm/models").json()
            assert any(m["name"] == "ai-test-echo" and m["is_default"] for m in models)
            assert all("api_key" not in str(m) for m in models)  # 列表不含凭证
            resp = client.post("/api/llm/complete", json={"prompt": "hi", "system": "sys"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["text"].startswith("[echo:") and "hi" in data["text"]
            assert data["cost_usd"] == 0.0
    finally:
        settings.rbac_enabled = False
        asyncio.run(_cleanup())


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        await _purge(pyp)
    finally:
        await pyp.dispose()
