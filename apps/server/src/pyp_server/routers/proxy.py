"""受控出口 REST API（11）：provider 管理 + 代理配置下拉项 + 溯源统计。均经 `proxy.manage`（管理员/运维）。

凭证信封加密存储（KEK 留平台侧，回显不含明文）。产出的「代理配置」供 02 数据源规则下拉选（rule.proxy_config_id）。
中转网关网络服务、真实 provider API 拉取延后（实现期按采购接入）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from payipa.db.engine import get_engine
from payipa.proxy import pool, store
from pydantic import BaseModel, Field

from pyp_server.auth import require_perm

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


class ProviderUpsertRequest(BaseModel):
    name: str = Field(..., description="provider 展示名（按 name upsert）")
    kind: str = Field(..., description="tunnel | longlived | iplist")
    api_config: dict[str, Any] = Field(..., description="明文凭证/参数 {endpoint|ips, protocol, ...}；入库前 KEK 加密")
    enabled: bool = Field(True, description="是否启用")


class ConfigCreateRequest(BaseModel):
    name: str = Field(..., description="代理配置名（数据源规则下拉显示）")
    mode: str = Field("single", description="single（单一直用）| mix（出口组）")
    provider_ids: list[int] = Field(default_factory=list, description="引用的 provider id 列表")
    no_proxy: bool = Field(False, description="不用代理（中转 passthrough 直连）")


@router.post(
    "/providers", summary="登记/更新 provider（凭证 KEK 加密）", dependencies=[Depends(require_perm("proxy.manage"))]
)
async def upsert_provider(body: ProviderUpsertRequest) -> dict:
    try:
        pid = await store.register_provider(
            get_engine("pyp"), name=body.name, kind=body.kind, api_config=body.api_config, enabled=body.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": pid}


@router.get("/providers", summary="provider 列表（不含凭证）", dependencies=[Depends(require_perm("proxy.manage"))])
async def list_providers() -> list[dict]:
    return await store.list_providers(get_engine("pyp"))


@router.post("/configs", summary="建代理配置下拉项", dependencies=[Depends(require_perm("proxy.manage"))])
async def create_config(body: ConfigCreateRequest) -> dict:
    try:
        cid = await store.create_config(
            get_engine("pyp"),
            name=body.name,
            mode=body.mode,
            provider_ids=body.provider_ids,
            no_proxy=body.no_proxy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": cid}


@router.get("/configs", summary="代理配置下拉项（02 规则页用）", dependencies=[Depends(require_perm("proxy.manage"))])
async def list_configs() -> list[dict]:
    return await store.list_configs(get_engine("pyp"))


@router.get(
    "/stats", summary="出口溯源统计（per-IP / per-(出口×域)）", dependencies=[Depends(require_perm("proxy.manage"))]
)
async def stats() -> dict:
    return await pool.egress_stats(get_engine("pyp"))
