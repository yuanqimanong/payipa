"""健康端点（P0-06）：/livez 存活、/readyz 可服务、/version 版本指纹。

「存活」≠「可服务」：/livez 零依赖（事件循环能应答即 200）；/readyz 逐项核验
配置、三库连通、迁移到 head、存储可写空间和后台环心跳，任一不绿返回 503 +
分项结果，编排器/负载均衡据此摘除流量。/healthz 保留为 /livez 别名（兼容既有冒烟）。
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from payipa.db.dynamic_schema import dynamic_schema_health
from payipa.db.engine import get_engine
from payipa.db.revisions import db_revision, script_head
from payipa.storage import get_storage
from payipa_contracts import CONTRACT_VERSION
from pydantic import BaseModel

from pyp_server.settings import get_server_settings

router = APIRouter(tags=["system"])

_DBS = ("pyp", "data_center", "business")
_CACHE_TTL_S = 2.0  # readyz 结果短缓存：编排器高频探针不放大三库压力
_CHECK_TIMEOUT_S = 2.0  # 每项检查独立限时，并行执行，故障库不能卡住整个探针。
# get_storage/script_head 均为进程级缓存资源；被取消的线程可能跨越 app 重启继续运行。
# 锁必须由真实工作线程持有到结束，不能随请求超时释放或随 lifespan 重建。
_BLOCKING_PROBE_LOCKS = {name: threading.Lock() for name in ("storage", "migrations")}


class ProbeInProgressError(RuntimeError):
    """上次阻塞检查尚未退出；当前请求不再重复调用底层资源。"""


def _run_blocking_probe[T](name: str, action: Callable[[], T]) -> T:
    lock = _BLOCKING_PROBE_LOCKS[name]
    if not lock.acquire(blocking=False):
        raise ProbeInProgressError(name)
    try:
        return action()
    finally:
        lock.release()


class Health(BaseModel):
    status: str = "ok"
    contract_version: int = CONTRACT_VERSION


@router.get("/livez", response_model=Health, summary="存活探针（零依赖，不连库）")
@router.get("/healthz", response_model=Health, summary="存活探针别名（兼容旧冒烟）")
async def livez() -> Health:
    return Health()


async def _ping_db(key: str) -> str:
    async with get_engine(key).connect() as conn:
        await conn.exec_driver_sql("SELECT 1")
    return "ok"


async def _check_migrations() -> str:
    head = await anyio.to_thread.run_sync(_run_blocking_probe, "migrations", script_head, abandon_on_cancel=True)
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


async def _check_dynamic_schemas() -> str:
    try:
        provisioning, failed = await dynamic_schema_health(get_engine("pyp"))
        if failed:
            return f"error: {failed} 个数据源 provisioning 失败"
        if provisioning:
            return f"error: {provisioning} 个数据源仍在 provisioning"
        return "ok"
    except Exception as exc:  # 迁移未到 head 时也必须返回分项错误，不能让 readyz 500
        return f"error: {type(exc).__name__}"


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
    cache = request.app.state.readiness_cache
    if cache["resp"] is not None and time.monotonic() - cache["at"] < _CACHE_TTL_S:
        return cache["resp"]
    # 刷新只由一个请求执行；等待者拿锁后重查缓存，取消或失败不会留下半份结果。
    async with request.app.state.readiness_lock:
        if cache["resp"] is not None and time.monotonic() - cache["at"] < _CACHE_TTL_S:
            return cache["resp"]
        checks = await _readiness_checks(request.app)
        ready = all(v in ("ok", "disabled") for v in checks.values())
        resp = JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "unavailable", "checks": checks},
        )
        cache.update(at=time.monotonic(), resp=resp)
        return resp


async def _readiness_checks(app) -> dict[str, str]:
    checks: dict[str, str] = {"config": "ok"}  # preflight 在启动时已把关；能跑到这就是过了
    cancellations: list[BaseException] = []

    async def check(name: str, action: Callable[[], Awaitable[str]]) -> None:
        try:
            with anyio.fail_after(_CHECK_TIMEOUT_S):
                checks[name] = await action()
        except Exception as exc:  # 取消继续向上传播；某项失败不影响其余探针。
            checks[name] = f"error: {type(exc).__name__}"
        except anyio.get_cancelled_exc_class() as exc:
            # TaskGroup 将子任务自行取消视为正常退出；保留信号，避免少查一项后误缓存 200。
            cancellations.append(exc)
            raise

    async def storage() -> str:
        # mkdir/disk_usage 可能阻塞（例如挂载卷）；探针超时后不等待底层文件系统恢复。
        return await anyio.to_thread.run_sync(_run_blocking_probe, "storage", _check_storage, abandon_on_cancel=True)

    async with anyio.create_task_group() as group:
        for key in _DBS:
            group.start_soon(check, f"db.{key}", lambda key=key: _ping_db(key))
        group.start_soon(check, "migrations", _check_migrations)
        group.start_soon(check, "dynamic_schemas", _check_dynamic_schemas)
        group.start_soon(check, "storage", storage)
    if cancellations:
        raise cancellations[0]
    settings = app.state.settings
    checks["loop.dispatch"] = _check_loop(app, "dispatch", settings.dispatch_enabled)
    checks["loop.consumer"] = _check_loop(app, "consumer", settings.push_enabled)
    return checks


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
