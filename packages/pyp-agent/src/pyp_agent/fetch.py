"""抓取引擎（01/07 定案）：按 ``engine_hint`` 选择采集引擎。

- **http**：niquests 直连（M1 起）。
- **browser**：标准 **Playwright**（M5）——动态渲染 / 已授权交互；agent 可选能力，惰性导入
  （未装 playwright extra 时 `browser_available()` 返回 False、注册时不上报 automation 能力，
  主控据能力分组只把 browser 任务派给有能力的 agent）。

网络出口由部署环境统一配置；任务和数据源不能动态修改出口。
访问边界（决策 2026-07-10）：认证失败/授权拒绝/交互式验证由上层判定为访问暂停，不在此自动重试换出口。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import niquests
from payipa_contracts import EngineHint

# Playwright 默认加载完成信号（DOM 就绪即可，避免长尾资源拖慢）。
_BROWSER_WAIT_UNTIL = "domcontentloaded"


@dataclass(slots=True)
class FetchResult:
    status: int
    url: str
    body: bytes
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def browser_available() -> bool:
    """agent 是否具备浏览器自动化能力（装了 playwright extra）。用于注册时上报 automation 能力。"""
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # noqa: BLE001 —— 未装/装坏都视为无能力（不阻断 http 采集）
        return False
    return True


async def _fetch_http(url: str, timeout: float, headers: dict[str, str] | None) -> FetchResult:
    async with niquests.AsyncSession() as session:
        resp = await session.get(url, timeout=timeout, headers=headers or {})
    return FetchResult(
        status=resp.status_code or 0,
        url=str(resp.url),
        body=resp.content or b"",
        content_type=resp.headers.get("content-type"),
        headers=dict(resp.headers),
    )


async def _fetch_browser(url: str, timeout: float, headers: dict[str, str] | None) -> FetchResult:
    """标准 Playwright 渲染取页（惰性导入）。渲染后回传 HTML（content-type 恒 text/html）。"""
    from playwright.async_api import async_playwright  # 惰性导入：仅 browser 任务才需运行时

    timeout_ms = int(timeout * 1000)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(extra_http_headers=headers or {})
            page = await context.new_page()
            resp = await page.goto(url, wait_until=_BROWSER_WAIT_UNTIL, timeout=timeout_ms)
            html = await page.content()
            status = resp.status if resp is not None else 0
            final_url = page.url
        finally:
            await browser.close()
    return FetchResult(
        status=status,
        url=final_url,
        body=html.encode("utf-8"),
        content_type="text/html; charset=utf-8",
        headers={},
    )


async def fetch(
    url: str,
    *,
    engine_hint: EngineHint = EngineHint.HTTP,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """按 engine_hint 取页。browser 需 agent 具备自动化能力（未装 playwright → 明确报错，由分组派发规避）。"""
    if engine_hint is EngineHint.HTTP:
        return await _fetch_http(url, timeout, headers)
    if engine_hint is EngineHint.BROWSER:
        if not browser_available():
            raise NotImplementedError(
                "browser 引擎需 playwright 运行时（uv sync 装 pyp-agent[browser] + playwright install chromium）；"
                "本 agent 无自动化能力——主控应按能力分组只派 http 任务给它"
            )
        return await _fetch_browser(url, timeout, headers)
    raise NotImplementedError(f"未知采集引擎 {engine_hint}")
