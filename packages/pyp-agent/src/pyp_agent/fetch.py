"""抓取引擎（M1：niquests 直连 http）。

按 ``engine_hint`` 选择采集引擎；当前实现常规 HTTP，标准浏览器能力留 M5。
M1 直连出网（代理中转随 M5）。全部经代理中转是 M5 的事，M1 不依赖未验证组件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import niquests
from payipa_contracts import EngineHint


@dataclass(slots=True)
class FetchResult:
    status: int
    url: str
    body: bytes
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


async def fetch(
    url: str,
    *,
    engine_hint: EngineHint = EngineHint.HTTP,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    if engine_hint is not EngineHint.HTTP:
        raise NotImplementedError(f"采集引擎 {engine_hint} 尚未实现；M5 接入标准 Playwright 浏览器能力")
    async with niquests.AsyncSession() as session:
        resp = await session.get(url, timeout=timeout, headers=headers or {})
    return FetchResult(
        status=resp.status_code or 0,
        url=str(resp.url),
        body=resp.content or b"",
        content_type=resp.headers.get("content-type"),
        headers=dict(resp.headers),
    )
