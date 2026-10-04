"""采集应用服务：显式注入三库依赖中的平台库与数据面，不依赖 Web 框架或全局配置。"""

from __future__ import annotations

from dataclasses import dataclass

from payipa_contracts import Channel, EngineHint, RulePack
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.crawl.batches import create_batch_with_requests
from payipa.crawl.rules import RuleStore
from payipa.crawl.sources import ensure_data_table, setup_source
from payipa.db.ident import check_code, check_field


@dataclass(frozen=True, slots=True)
class SourceRun:
    """已持久化的运行结果；实际派发由后台队列完成。"""

    batch_id: int
    requests: int
    dispatched: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"batch_id": self.batch_id, "requests": self.requests, "dispatched": self.dispatched}


@dataclass(frozen=True, slots=True)
class CrawlService:
    """建源 → 规则版本 → 动态表 → 批次。构造服务不会连接数据库。"""

    pyp: AsyncEngine
    data_center: AsyncEngine

    async def run_source(
        self,
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
    ) -> SourceRun:
        channel = Channel(channel)
        # 标识符先校验，避免非法动态表名/字段在写入源与规则后才暴露。
        check_code(uuid)
        fields_indexed = indexed_fields or [f.name for f in rule.fields if f.index]
        for field in fields_indexed:
            check_field(field)
        source_id, task_id = await setup_source(
            self.pyp,
            uuid,
            name,
            seed_urls=seed_urls,
            access_basis=access_basis,
            access_reference=access_reference,
            access_confirmed=access_confirmed,
            engine_hint=engine_hint,
            rate_limit=rate_limit,
            retry=retry,
            timeout=timeout,
            raw_archive=raw_archive,
        )
        pointer = await RuleStore(self.pyp).put(source_id, rule)
        # 正式表是长期基线；试跑另建物理隔离表，随后 cron/prod 重跑也有正式表可用。
        await ensure_data_table(self.data_center, uuid, fields_indexed, engine_pyp=self.pyp, channel=Channel.PROD)
        if channel is Channel.TEST:
            await ensure_data_table(self.data_center, uuid, fields_indexed, engine_pyp=self.pyp, channel=Channel.TEST)
        batch_id, specs = await create_batch_with_requests(
            self.pyp, task_id=task_id, source_uuid=uuid, targets=seed_urls, rule_ptr=pointer, channel=channel
        )
        return SourceRun(batch_id=batch_id, requests=len(specs))
