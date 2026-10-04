"""Web 装配适配器：绑定数据库依赖，兼容原有 dispatch_source_run 调用。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from payipa.crawl.service import CrawlService
from payipa.db.engine import get_engine
from payipa_contracts import Channel, EngineHint, RulePack


def get_crawl_service() -> CrawlService:
    """可用于 FastAPI Depends 的服务工厂；引擎懒建，构造过程不连库。"""
    return CrawlService(pyp=get_engine("pyp"), data_center=get_engine("data_center"))


CrawlServiceDependency = Annotated[CrawlService, Depends(get_crawl_service)]


async def dispatch_source_run(
    *,
    uuid: str,
    name: str,
    seed_urls: list[str],
    rule: RulePack,
    indexed_fields: list[str] | None = None,
    channel: Channel = Channel.PROD,
    access_basis: str | None = None,
    access_reference: str | None = None,
    access_confirmed: bool = False,
    engine_hint: EngineHint | None = None,
    rate_limit: int | None = None,
    retry: int | None = None,
    timeout: int | None = None,
    raw_archive: bool | None = None,
    service: CrawlService | None = None,
) -> dict:
    """建源+存规则+建表+建批次；请求以 QUEUED 落库，实际下发由后台派发环负责。

    返回 {batch_id, requests, dispatched}；``dispatched`` 恒为 0——派发不再在此同步发生，
    避免「空闲槽不够就丢请求」的一次性派发缺陷（M1 遗留）。
    """
    result = await (service if service is not None else get_crawl_service()).run_source(
        uuid=uuid,
        name=name,
        seed_urls=seed_urls,
        rule=rule,
        indexed_fields=indexed_fields,
        channel=channel,
        access_basis=access_basis,
        access_reference=access_reference,
        access_confirmed=access_confirmed,
        engine_hint=engine_hint,
        rate_limit=rate_limit,
        retry=retry,
        timeout=timeout,
        raw_archive=raw_archive,
    )
    return result.as_dict()
