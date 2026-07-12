"""抓取引擎（01/07 定案）：按 ``engine_hint`` 选择采集引擎。

- **http**：niquests 直连（M1 起）。
- **browser**：标准 **Playwright**（M5）——动态渲染 / 已授权交互；agent 可选能力，惰性导入
  （未装 playwright extra 时 `browser_available()` 返回 False、注册时不上报 automation 能力，
  主控据能力分组只把 browser 任务派给有能力的 agent）。

网络出口由部署环境统一配置；任务和数据源不能动态修改出口。
访问边界（决策 2026-07-10）：认证失败/授权拒绝/交互式验证由上层判定为访问暂停，不在此自动重试换出口。
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import niquests
from payipa_contracts import EngineHint

from pyp_agent.url_policy import PublicAddressResolver, URLPolicyError, browser_pinned_hosts, validate_url

# Playwright 默认加载完成信号（DOM 就绪即可，避免长尾资源拖慢）。
_BROWSER_WAIT_UNTIL = "domcontentloaded"

# 响应体上限：默认 10MB，环境变量 PYP_AGENT_MAX_BODY_MB 或 fetch(max_bytes=) 可调（防超大页 OOM）。
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
_HTTP_CHUNK = 64 * 1024


def max_body_bytes() -> int:
    """响应体上限（字节）。PYP_AGENT_MAX_BODY_MB 可调；缺失/非法/非正值回落默认 10MB。"""
    try:
        mb = int(os.environ.get("PYP_AGENT_MAX_BODY_MB", ""))
    except ValueError:
        return _DEFAULT_MAX_BODY_BYTES
    return mb * 1024 * 1024 if mb > 0 else _DEFAULT_MAX_BODY_BYTES


@dataclass(slots=True)
class FetchResult:
    status: int
    url: str
    body: bytes
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class FetchFailure(RuntimeError):
    """采集传输层失败；异常文本不包含目标 URL、请求头或响应正文。"""


class FetchTimeout(FetchFailure):
    """目标连接或读取超过任务时限。"""


class FetchNetworkError(FetchFailure):
    """DNS、连接、TLS 或连接重置等无有效 HTTP 响应的故障。"""


class FetchTooLarge(FetchFailure):
    """响应体超出上限：超限即停读断开，绝不整页拉进内存。"""


def browser_available() -> bool:
    """agent 是否具备浏览器自动化能力（装了 playwright extra）。用于注册时上报 automation 能力。"""
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # noqa: BLE001 —— 未装/装坏都视为无能力（不阻断 http 采集）
        return False
    return True


async def probe_browser_runtime() -> bool:
    """真实启动一次 Chromium；只有 import 与 launch 都成功才允许上报 browser 能力。"""
    if not browser_available():
        return False
    try:
        import playwright.async_api as playwright_api

        async with playwright_api.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
    except Exception:  # noqa: BLE001
        return False
    return True


_MAX_REDIRECTS = 5


async def _fetch_http(
    url: str,
    timeout: float,
    headers: dict[str, str] | None,
    max_bytes: int,
    allowed_domains: list[str],
) -> FetchResult:
    """手工跟随重定向：库内部跟随会为复用连接**整段读完**中间 3xx 的响应体，
    敌意超大 3xx 可绕过字节上限拖爆内存——这里每一跳都不读体、直接断开换下一跳。"""
    from urllib.parse import urljoin

    resolver = PublicAddressResolver(allowed_domains)
    try:
        async with niquests.AsyncSession(resolver=resolver) as session:
            cur = url
            for _hop in range(_MAX_REDIRECTS + 1):
                await validate_url(cur, allowed_domains)
                resp = await session.get(
                    cur, timeout=timeout, headers=headers or {}, stream=True, allow_redirects=False
                )
                location = resp.headers.get("location")
                if 300 <= (resp.status_code or 0) < 400 and location:
                    await resp.close()  # 重定向体一个字节都不读
                    cur = urljoin(cur, location)
                    continue
                declared = resp.headers.get("content-length") or ""
                if declared.isdigit() and int(declared) > max_bytes:  # 头先拒：一个字节都不读
                    await resp.close()
                    raise FetchTooLarge(f"response Content-Length exceeds {max_bytes} bytes cap")
                chunks: list[bytes] = []
                total = 0
                async for chunk in await resp.iter_content(_HTTP_CHUNK):  # 流式累积，防 Content-Length 缺失/说谎
                    total += len(chunk)
                    if total > max_bytes:
                        await resp.close()  # 超限立即断开，不再继续读
                        raise FetchTooLarge(f"response body exceeds {max_bytes} bytes cap")
                    chunks.append(chunk)
                return FetchResult(
                    status=resp.status_code or 0,
                    url=cur,
                    body=b"".join(chunks),
                    content_type=resp.headers.get("content-type"),
                    headers={str(k).lower(): str(v) for k, v in resp.headers.items()},
                )
            raise FetchNetworkError(f"too many redirects (> {_MAX_REDIRECTS})")
    except niquests.exceptions.Timeout as exc:
        raise FetchTimeout(f"HTTP request timed out after {timeout:g}s") from exc
    except niquests.exceptions.RequestException as exc:
        raise FetchNetworkError(f"HTTP transport failed ({type(exc).__name__})") from exc
    finally:
        with contextlib.suppress(Exception):
            await resolver.close()


async def _fetch_browser(
    url: str,
    timeout: float,
    headers: dict[str, str] | None,
    max_bytes: int,
    allowed_domains: list[str],
) -> FetchResult:
    """标准 Playwright 渲染取页（惰性导入）。渲染后回传 HTML（content-type 恒 text/html）。"""
    import playwright.async_api as playwright_api  # 惰性导入：仅 browser 任务才需运行时

    async_playwright = playwright_api.async_playwright
    # 兼容测试桩和裁剪运行时；官方 Playwright 均提供这两个类型。
    PlaywrightTimeoutError = getattr(playwright_api, "TimeoutError", TimeoutError)
    PlaywrightError = getattr(playwright_api, "Error", Exception)

    timeout_ms = int(timeout * 1000)
    try:
        await validate_url(url, allowed_domains)
        pinned_hosts = await browser_pinned_hosts(url, allowed_domains)
        rules = []
        for host, address in pinned_hosts.items():
            destination = f"[{address}]" if ":" in address else address
            rules.append(f"MAP {host} {destination}")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules={','.join(rules)}"],
            )
            try:
                context = await browser.new_context(extra_http_headers=headers or {})

                async def guard_route(route) -> None:
                    try:
                        host = (urlsplit(route.request.url).hostname or "").encode("idna").decode("ascii").lower()
                        if host not in pinned_hosts:
                            raise URLPolicyError("browser target host is not explicitly pinned")
                        await validate_url(route.request.url, allowed_domains)
                    except ValueError:
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_()

                await context.route("**/*", guard_route)
                page = await context.new_page()
                resp = await page.goto(url, wait_until=_BROWSER_WAIT_UNTIL, timeout=timeout_ms)
                html = await page.content()
                status = resp.status if resp is not None else 0
                final_url = page.url
                await validate_url(final_url, allowed_domains)
                final_host = (urlsplit(final_url).hostname or "").encode("idna").decode("ascii").lower()
                if final_host not in pinned_hosts:
                    raise URLPolicyError("browser final host is not explicitly pinned")
                all_headers = getattr(resp, "all_headers", None) if resp is not None else None
                response_headers = await all_headers() if all_headers is not None else {}
            finally:
                await browser.close()
    except URLPolicyError:
        raise
    except PlaywrightTimeoutError as exc:
        raise FetchTimeout(f"browser navigation timed out after {timeout:g}s") from exc
    except PlaywrightError as exc:
        raise FetchNetworkError(f"browser transport failed ({type(exc).__name__})") from exc
    body = html.encode("utf-8")
    if len(body) > max_bytes:  # Playwright 只能整页取回，渲染后立刻校验，超限不再向下游传播
        raise FetchTooLarge(f"rendered page exceeds {max_bytes} bytes cap")
    return FetchResult(
        status=status,
        url=final_url,
        body=body,
        content_type="text/html; charset=utf-8",
        headers={str(k).lower(): str(v) for k, v in response_headers.items()},
    )


async def fetch(
    url: str,
    *,
    engine_hint: EngineHint = EngineHint.HTTP,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    allowed_domains: list[str] | None = None,
) -> FetchResult:
    """按 engine_hint 取页。browser 需 agent 具备自动化能力（未装 playwright → 明确报错，由分组派发规避）。

    max_bytes：响应体上限（None → 取 PYP_AGENT_MAX_BODY_MB / 默认 10MB）；超限抛 FetchTooLarge。
    """
    cap = max_bytes if max_bytes is not None else max_body_bytes()
    boundary = list(allowed_domains or [])
    if not boundary and urlsplit(url).hostname:
        boundary = [str(urlsplit(url).hostname)]
    if engine_hint is EngineHint.HTTP:
        return await _fetch_http(url, timeout, headers, cap, boundary)
    if engine_hint is EngineHint.BROWSER:
        if not browser_available():
            raise NotImplementedError(
                "browser 引擎需 playwright 运行时（uv sync 装 pyp-agent[browser] + playwright install chromium）；"
                "本 agent 无自动化能力——主控应按能力分组只派 http 任务给它"
            )
        return await _fetch_browser(url, timeout, headers, cap, boundary)
    raise NotImplementedError(f"未知采集引擎 {engine_hint}")
