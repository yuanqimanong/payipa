"""出口组选路 + 溯源统计（11 §定案-4/6/7）。

`select_egress(config_id)`：解析代理配置 → no_proxy=passthrough / single=首个 provider / mix=按健康选一个 provider
→ adapter.acquire_egress。健康仅依据**传输信号**（连接/超时/供应商可用性），应用层访问拒绝不触发出口切换。
`record_usage`：每次请求记 provider/出口 IP/目标域/account/成败/耗时/状态码。
`egress_stats`：聚合 per-IP 使用次数与成功率、per-(出口×域×account) 成功率 → 喂 07 调频与 monitor（ProxyStat）。

中转网关（出口数据面 B）作为可独立部署的网络转发服务延后；本模块产出「选哪个出口」+「统计」两件事。
"""

from __future__ import annotations

from typing import Any

from payipa_contracts import ProxyStat
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import ProxyUsage
from payipa.proxy.adapters import PASSTHROUGH, Egress, NoEgressAvailable, build_adapter
from payipa.proxy.store import get_config, resolve_provider

MIN_SAMPLES_FOR_HEALTH = 5  # 样本不足则视为健康（不因偶发失败误剔除新出口）
HEALTH_SUCCESS_FLOOR = 0.5  # 传输成功率低于此的出口 IP 判不健康（降权/剔除）


async def _unhealthy_ips(engine_pyp: AsyncEngine, provider_name: str | None) -> set[str]:
    """按 proxy_usage 传输成败聚合，返回成功率低于地板且样本足够的出口 IP 集合（用于健康过滤）。"""
    async with engine_pyp.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    ProxyUsage.egress_ip,
                    func.count().label("n"),
                    func.coalesce(func.sum(cast(ProxyUsage.success, Integer)), 0).label("ok"),
                )
                .where(ProxyUsage.egress_ip.is_not(None))
                .group_by(ProxyUsage.egress_ip)
            )
        ).all()
    bad: set[str] = set()
    for ip, n, ok in rows:
        if int(n) >= MIN_SAMPLES_FOR_HEALTH and int(ok) / int(n) < HEALTH_SUCCESS_FLOOR:
            bad.add(ip)
    return bad


async def select_egress(engine_pyp: AsyncEngine, config_id: int, target: str, *, kek: str | None = None) -> Egress:
    """按代理配置为 target 选一个出口。no_proxy→passthrough；single→首个 provider；mix→按健康轮换 provider。

    无可用健康出口 → NoEgressAvailable。配置不存在 → ValueError。
    """
    config = await get_config(engine_pyp, config_id)
    if config is None:
        raise ValueError(f"代理配置 {config_id} 不存在")
    refs = config["provider_refs"]
    if refs.get("no_proxy"):
        return PASSTHROUGH
    provider_ids: list[int] = list(refs.get("provider_ids") or [])
    if not provider_ids:
        return PASSTHROUGH  # 空配置等价不用代理
    bad = await _unhealthy_ips(engine_pyp, None)

    def healthy(ip: str) -> bool:
        host = ip.rsplit(":", 1)[0] if ":" in ip else ip
        return host not in bad and ip not in bad

    # single 取第一个 provider；mix 依次尝试各 provider 直到取到健康出口
    order = provider_ids[:1] if config["mode"] == "single" else provider_ids
    last_err: Exception | None = None
    for pid in order:
        prov = await resolve_provider(engine_pyp, pid, kek=kek)
        adapter = build_adapter(prov["kind"], prov["config"])
        try:
            if prov["kind"] == "tunnel":
                return adapter.acquire_egress(target)  # 隧道出口 IP 未知、不做本地健康过滤
            return adapter.acquire_egress(target, healthy=healthy)  # type: ignore[call-arg]
        except NoEgressAvailable as exc:
            last_err = exc
            continue
    raise NoEgressAvailable(f"代理配置 {config_id} 无健康出口") from last_err


async def record_usage(
    engine_pyp: AsyncEngine,
    *,
    egress_ip: str | None,
    target_domain: str | None,
    success: bool,
    account: str | None = None,
    latency_ms: int | None = None,
    status_code: int | None = None,
) -> None:
    """记一条出口使用（溯源）。success 表示**传输**成败（应用层拒绝不记为出口失败）。"""
    async with engine_pyp.begin() as conn:
        await conn.execute(
            pg_insert(ProxyUsage.__table__).values(
                egress_ip=egress_ip,
                target_domain=target_domain,
                account=account,
                success=success,
                latency=latency_ms,
                status_code=status_code,
            )
        )


async def egress_stats(engine_pyp: AsyncEngine) -> dict[str, Any]:
    """聚合溯源：per-IP 使用次数/成功率 + per-(出口×域) 成功率（喂 07 调频与 monitor）。"""
    async with engine_pyp.connect() as conn:
        per_ip = (
            await conn.execute(
                select(
                    ProxyUsage.egress_ip,
                    func.count().label("n"),
                    func.coalesce(func.sum(cast(ProxyUsage.success, Integer)), 0).label("ok"),
                )
                .where(ProxyUsage.egress_ip.is_not(None))
                .group_by(ProxyUsage.egress_ip)
            )
        ).all()
        per_ed = (
            await conn.execute(
                select(
                    ProxyUsage.egress_ip,
                    ProxyUsage.target_domain,
                    func.count().label("n"),
                    func.coalesce(func.sum(cast(ProxyUsage.success, Integer)), 0).label("ok"),
                )
                .where(ProxyUsage.egress_ip.is_not(None), ProxyUsage.target_domain.is_not(None))
                .group_by(ProxyUsage.egress_ip, ProxyUsage.target_domain)
            )
        ).all()
    by_ip = {ip: {"count": int(n), "success_rate": round(int(ok) / int(n), 4)} for ip, n, ok in per_ip if int(n)}
    by_egress_domain = {f"{ip}|{domain}": round(int(ok) / int(n), 4) for ip, domain, n, ok in per_ed if int(n)}
    return {"by_ip": by_ip, "by_egress_domain": by_egress_domain}


async def proxy_stat(engine_pyp: AsyncEngine) -> ProxyStat:
    """monitor 用：per-(出口×域) 成功率 → contracts ProxyStat。"""
    stats = await egress_stats(engine_pyp)
    return ProxyStat(by_egress_domain=stats["by_egress_domain"])


__all__ = ["egress_stats", "proxy_stat", "record_usage", "select_egress"]
