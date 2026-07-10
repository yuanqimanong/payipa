"""受控出口的配置存储（11 §定案-1/3）：provider（凭证信封加密）+ 代理配置（下拉项）。

- `proxy_providers`：name / kind(tunnel|longlived|iplist) / api_config(JSONB 密文) / enabled。
- `proxy_configs`：name / mode(single|mix) / provider_refs(JSONB) + owner。
  provider_refs 约定：``{"provider_ids": [..], "no_proxy": bool}``；no_proxy=True → passthrough 出口。

产出「代理配置」下拉项供 02 数据源规则选用（出口组 / 单一 provider / 不用代理）。api_config 明文不落库。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import ProxyConfig, ProxyProvider
from payipa.security.secrets import decrypt_json, encrypt_json

_KINDS = ("tunnel", "longlived", "iplist")


async def register_provider(
    engine_pyp: AsyncEngine,
    *,
    name: str,
    kind: str,
    api_config: dict[str, Any],
    enabled: bool = True,
    kek: str | None = None,
) -> int:
    """登记/更新一个 provider（按 name upsert）。api_config 明文经 KEK 信封加密入库。"""
    if kind not in _KINDS:
        raise ValueError(f"provider kind 须为 {_KINDS}，得到 {kind!r}")
    enc = encrypt_json(api_config, kek=kek)
    async with engine_pyp.begin() as conn:
        existing = (await conn.execute(select(ProxyProvider.id).where(ProxyProvider.name == name))).scalar()
        if existing is None:
            return int(
                (
                    await conn.execute(
                        pg_insert(ProxyProvider.__table__)
                        .values(name=name, kind=kind, api_config={"enc": enc}, enabled=enabled)
                        .returning(ProxyProvider.id)
                    )
                ).scalar_one()
            )
        await conn.execute(
            update(ProxyProvider.__table__)
            .where(ProxyProvider.id == existing)
            .values(kind=kind, api_config={"enc": enc}, enabled=enabled)
        )
        return int(existing)


async def resolve_provider(engine_pyp: AsyncEngine, provider_id: int, *, kek: str | None = None) -> dict[str, Any]:
    """解析 provider：解密 api_config，返回 ``{id, name, kind, config}``。禁用/不存在/无凭证 → ValueError。"""
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(ProxyProvider.name, ProxyProvider.kind, ProxyProvider.api_config, ProxyProvider.enabled).where(
                    ProxyProvider.id == provider_id
                )
            )
        ).first()
    if row is None:
        raise ValueError(f"proxy provider {provider_id} 不存在")
    name, kind, api_col, enabled = row
    if not enabled:
        raise ValueError(f"proxy provider '{name}' 已禁用")
    enc = (api_col or {}).get("enc")
    config = decrypt_json(enc, kek=kek) if enc else {}
    config.setdefault("name", name)
    return {"id": provider_id, "name": name, "kind": kind, "config": config}


async def create_config(
    engine_pyp: AsyncEngine,
    *,
    name: str,
    mode: str = "single",
    provider_ids: list[int] | None = None,
    no_proxy: bool = False,
    owner_id: int | None = None,
) -> int:
    """建一条「代理配置」下拉项（出口组 mix / 单一 single / 不用代理 no_proxy）。返回 config id。"""
    if mode not in ("single", "mix"):
        raise ValueError(f"proxy config mode 须为 single|mix，得到 {mode!r}")
    refs = {"provider_ids": list(provider_ids or []), "no_proxy": bool(no_proxy)}
    async with engine_pyp.begin() as conn:
        return int(
            (
                await conn.execute(
                    pg_insert(ProxyConfig.__table__)
                    .values(name=name, mode=mode, provider_refs=refs, owner_id=owner_id)
                    .returning(ProxyConfig.id)
                )
            ).scalar_one()
        )


async def get_config(engine_pyp: AsyncEngine, config_id: int) -> dict[str, Any] | None:
    """取一条代理配置（不解密 provider 凭证）；不存在返回 None。"""
    async with engine_pyp.connect() as conn:
        row = (
            await conn.execute(
                select(ProxyConfig.id, ProxyConfig.name, ProxyConfig.mode, ProxyConfig.provider_refs).where(
                    ProxyConfig.id == config_id
                )
            )
        ).first()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "mode": row[2], "provider_refs": row[3] or {}}


async def list_configs(engine_pyp: AsyncEngine) -> list[dict[str, Any]]:
    """列出代理配置下拉项（供 02 规则页；不含凭证）。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(ProxyConfig.id, ProxyConfig.name, ProxyConfig.mode, ProxyConfig.provider_refs).order_by(
                    ProxyConfig.id
                )
            )
        ).all()
    return [
        {
            "id": i,
            "name": n,
            "mode": m,
            "no_proxy": bool((r or {}).get("no_proxy")),
            "provider_ids": (r or {}).get("provider_ids", []),
        }
        for i, n, m, r in rows
    ]


async def list_providers(engine_pyp: AsyncEngine) -> list[dict[str, Any]]:
    """列出 provider 元信息（不含凭证明文）。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(ProxyProvider.id, ProxyProvider.name, ProxyProvider.kind, ProxyProvider.enabled).order_by(
                    ProxyProvider.id
                )
            )
        ).all()
    return [{"id": i, "name": n, "kind": k, "enabled": bool(e)} for i, n, k, e in rows]


__all__ = [
    "create_config",
    "get_config",
    "list_configs",
    "list_providers",
    "register_provider",
    "resolve_provider",
]
