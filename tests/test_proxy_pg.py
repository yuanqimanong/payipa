"""M5 受控出口/代理池集成测试（需 PG）：provider 凭证信封 + 代理配置下拉项 + 出口组选路（健康过滤）
+ 溯源统计 + HTTP proxy.manage 闸门。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from payipa.db.pyp import User
from payipa.db.settings import get_settings
from payipa.proxy import adapters, pool, store
from payipa.security.rbac import assign_role, seed_default_rbac
from payipa.security.secrets import decrypt_json
from pyp_server.auth import COOKIE_NAME, create_session
from pyp_server.main import app
from pyp_server.settings import get_server_settings
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine


async def _purge(pyp) -> None:
    async with pyp.begin() as conn:
        await conn.execute(text("DELETE FROM proxy_usage"))
        await conn.execute(text("DELETE FROM proxy_configs WHERE name LIKE 'px-test-%'"))
        await conn.execute(text("DELETE FROM proxy_providers WHERE name LIKE 'px-test-%'"))
        await conn.execute(
            text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'px-%')")
        )
        await conn.execute(text("DELETE FROM users WHERE username LIKE 'px-%'"))


def test_proxy_store_select_and_stats(require_pg: None) -> None:
    async def main() -> dict:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            # provider：隧道（固定入口）+ iplist（两个 IP）。凭证应加密入库。
            tun = await store.register_provider(
                pyp,
                name="px-test-tunnel",
                kind="tunnel",
                api_config={"endpoint": "relay.example:8000", "protocol": "http", "token": "secret-t"},
            )
            ipl = await store.register_provider(
                pyp,
                name="px-test-iplist",
                kind="iplist",
                api_config={"ips": ["1.1.1.1:9000", "2.2.2.2:9000"], "protocol": "http"},
            )
            async with pyp.connect() as conn:
                raw = (
                    await conn.execute(text("SELECT api_config FROM proxy_providers WHERE id=:i"), {"i": tun})
                ).scalar()
            assert "secret-t" not in str(raw)  # 明文不落库
            assert decrypt_json(raw["enc"])["token"] == "secret-t"  # 可解回

            # 三种配置：single(tunnel) / mix(iplist) / no_proxy
            c_single = await store.create_config(pyp, name="px-test-single", mode="single", provider_ids=[tun])
            c_mix = await store.create_config(pyp, name="px-test-mix", mode="mix", provider_ids=[ipl])
            c_none = await store.create_config(pyp, name="px-test-none", no_proxy=True)

            # 选路：no_proxy → passthrough
            e_none = await pool.select_egress(pyp, c_none, "books.example")
            # single tunnel → 固定入口，ip 未知
            e_tun = await pool.select_egress(pyp, c_single, "books.example")
            # iplist → 取一个 IP
            e_ip = await pool.select_egress(pyp, c_mix, "books.example")

            # 健康过滤：把 1.1.1.1 打成传输失败（≥5 样本、成功率 0）→ 选路应避开它，只剩 2.2.2.2
            for _ in range(6):
                await pool.record_usage(
                    pyp, egress_ip="1.1.1.1", target_domain="books.example", success=False, latency_ms=10
                )
            for _ in range(6):
                await pool.record_usage(
                    pyp, egress_ip="2.2.2.2", target_domain="books.example", success=True, latency_ms=10
                )
            picks = {(await pool.select_egress(pyp, c_mix, "books.example")).ip for _ in range(4)}

            stats = await pool.egress_stats(pyp)
            pstat = await pool.proxy_stat(pyp)
            return {
                "e_none": e_none,
                "e_tun": e_tun,
                "e_ip": e_ip,
                "picks": picks,
                "stats": stats,
                "pstat": pstat,
            }
        finally:
            await _purge(pyp)
            await pyp.dispose()

    out = asyncio.run(main())
    assert out["e_none"].passthrough is True and out["e_none"].endpoint is None
    assert out["e_tun"].endpoint == "relay.example:8000" and out["e_tun"].ip is None
    assert out["e_ip"].ip in {"1.1.1.1", "2.2.2.2"}
    # 1.1.1.1 不健康 → 选路只落 2.2.2.2
    assert out["picks"] == {"2.2.2.2"}
    # 溯源：per-IP 成功率
    assert out["stats"]["by_ip"]["1.1.1.1"] == {"count": 6, "success_rate": 0.0}
    assert out["stats"]["by_ip"]["2.2.2.2"]["success_rate"] == 1.0
    assert out["stats"]["by_egress_domain"]["2.2.2.2|books.example"] == 1.0
    assert out["pstat"].by_egress_domain["1.1.1.1|books.example"] == 0.0


def test_no_healthy_egress_raises(require_pg: None) -> None:
    """出口组全部不健康 → NoEgressAvailable（不静默直连）。"""

    async def main() -> bool:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            ipl = await store.register_provider(
                pyp, name="px-test-iplist", kind="iplist", api_config={"ips": ["9.9.9.9:1"], "protocol": "http"}
            )
            cfg = await store.create_config(pyp, name="px-test-mix", mode="mix", provider_ids=[ipl])
            for _ in range(6):
                await pool.record_usage(pyp, egress_ip="9.9.9.9", target_domain="d", success=False)
            try:
                await pool.select_egress(pyp, cfg, "d")
                return False
            except adapters.NoEgressAvailable:
                return True
        finally:
            await _purge(pyp)
            await pyp.dispose()

    assert asyncio.run(main()) is True


def test_proxy_endpoints_gated(require_pg: None) -> None:
    """HTTP：proxy.manage 闸门（运维有/运营无）+ provider/config/stats 端到端。"""

    async def seed() -> dict[str, int]:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        try:
            await _purge(pyp)
            await seed_default_rbac(pyp)
            ids: dict[str, int] = {}
            async with pyp.begin() as conn:
                for name in ("px-sre", "px-op"):
                    uid = (
                        await conn.execute(
                            pg_insert(User.__table__)
                            .values(username=name, password_hash="x", status="active")
                            .returning(User.id)
                        )
                    ).scalar_one()
                    ids[name] = int(uid)
            await assign_role(pyp, ids["px-sre"], "运维")  # 运维矩阵含 proxy.manage
            await assign_role(pyp, ids["px-op"], "运营")  # 运营不含
            return ids
        finally:
            await pyp.dispose()

    ids = asyncio.run(seed())
    settings = get_server_settings()
    settings.rbac_enabled = True
    try:
        with TestClient(app) as client:
            body = {"name": "px-test-tunnel", "kind": "tunnel", "api_config": {"endpoint": "r:8000"}}
            assert client.post("/api/proxy/providers", json=body).status_code == 401
            client.cookies.set(COOKIE_NAME, create_session(ids["px-op"], "px-op"))
            assert client.post("/api/proxy/providers", json=body).status_code == 403
            client.cookies.set(COOKIE_NAME, create_session(ids["px-sre"], "px-sre"))
            r = client.post("/api/proxy/providers", json=body)
            assert r.status_code == 200
            pid = r.json()["id"]
            provs = client.get("/api/proxy/providers").json()
            assert any(p["id"] == pid and p["kind"] == "tunnel" for p in provs)
            assert all("endpoint" not in str(p) for p in provs)  # 不回显凭证
            cfg = client.post(
                "/api/proxy/configs", json={"name": "px-test-single", "mode": "single", "provider_ids": [pid]}
            )
            assert cfg.status_code == 200
            configs = client.get("/api/proxy/configs").json()
            assert any(c["name"] == "px-test-single" for c in configs)
            assert client.get("/api/proxy/stats").status_code == 200
    finally:
        settings.rbac_enabled = False
        asyncio.run(_cleanup())


async def _cleanup() -> None:
    pyp = create_async_engine(get_settings().async_url("pyp"))
    try:
        await _purge(pyp)
    finally:
        await pyp.dispose()
