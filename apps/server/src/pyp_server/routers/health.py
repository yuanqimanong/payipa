"""健康端点（P0-06）：/livez 存活、/readyz 可服务、/version 版本指纹。

「存活」≠「可服务」：/livez 零依赖（事件循环能应答即 200）；/readyz 逐项核验
配置、三库连通、迁移到 head、存储可写空间和后台环心跳，任一不绿返回 503 +
分项结果，编排器/负载均衡据此摘除流量。/healthz 保留为 /livez 别名（兼容既有冒烟）。
"""

from __future__ import annotations

import subprocess
import time
from functools import lru_cache

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from payipa.db.engine import get_engine
from payipa.db.revisions import db_revision, script_head
from payipa.storage import get_storage
from payipa_contracts import CONTRACT_VERSION
from pydantic import BaseModel

from pyp_server.settings import get_server_settings

router = APIRouter(tags=["system"])

_DBS = ("pyp", "data_center", "business")
_CACHE_TTL_S = 2.0  # readyz 结果短缓存：编排器高频探针不放大三库压力
_ready_cache: dict = {"at": 0.0, "resp": None}


class Health(BaseModel):
    status: str = "ok"
    contract_version: int = CONTRACT_VERSION


@router.get("/livez", response_model=Health, summary="存活探针（零依赖，不连库）")
@router.get("/healthz", response_model=Health, summary="存活探针别名（兼容旧冒烟）")
async def livez() -> Health:
    return Health()


async def _ping_db(key: str) -> str:
    try:
        with anyio.fail_after(2):
            async with get_engine(key).connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


async def _check_migrations() -> str:
    head = script_head()
    if head is None:
        return "error: 迁移脚本目录不可用（须从仓库根运行或携带 deploy/）"
    for key in _DBS:
        rev = await db_revision(get_engine(key))
        if rev != head:
            return f"error: {key} 在 {rev or '未初始化'}，期望 head {head}"
    return "ok"


def _check_storage() -> str:
    try:
        store = get_storage()
        if hasattr(store, "disk_ok") and not store.disk_ok():
            return "error: 剩余磁盘空间低于水位"
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


def _check_loop(app, name: str, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    health = getattr(app.state, "loop_health", {}).get(name)
    if health is None:
        return "error: 循环未启动"
    if not health.fresh():
        if health.last_ok_at is None:
            return "error: 后台环未运行（未获单实例锁或数据库未就绪）"
        return f"error: 心跳过期（连续失败 {health.consecutive_fails}：{health.last_error or '无错误记录'}）"
    return "ok"


@router.get("/readyz", summary="可服务探针（三库/迁移/存储/后台环全绿才 200）")
async def readyz(request: Request) -> JSONResponse:
    now = time.monotonic()
    if _ready_cache["resp"] is not None and now - _ready_cache["at"] < _CACHE_TTL_S:
        return _ready_cache["resp"]
    settings = get_server_settings()
    checks: dict[str, str] = {"config": "ok"}  # preflight 在启动时已把关；能跑到这就是过了
    for key in _DBS:
        checks[f"db.{key}"] = await _ping_db(key)
    checks["migrations"] = await _check_migrations()
    checks["storage"] = _check_storage()
    checks["loop.dispatch"] = _check_loop(request.app, "dispatch", settings.dispatch_enabled)
    checks["loop.consumer"] = _check_loop(request.app, "consumer", settings.push_enabled)
    ready = all(v in ("ok", "disabled") for v in checks.values())
    resp = JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "unavailable", "checks": checks},
    )
    _ready_cache.update(at=now, resp=resp)
    return resp


@lru_cache
def _git_commit() -> str:
    """dev 环境兜底取 commit（1s 超时）；正式镜像应经 PYP_SERVER_BUILD_COMMIT 注入。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=1, check=False
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 —— 无 git/超时一律 unknown
        return "unknown"


@router.get("/version", summary="版本指纹（server/contracts/commit/迁移 revision）")
async def version() -> dict:
    settings = get_server_settings()
    schema: dict[str, str | None] = {"expected_head": script_head()}
    for key in _DBS:  # best-effort：库不可达为 null，/version 永不 500
        schema[key] = await db_revision(get_engine(key))
    return {
        "server": settings.version,
        "contracts": CONTRACT_VERSION,
        "commit": settings.build_commit or _git_commit(),
        "build_time": settings.build_time or None,
        "schema": schema,
    }
