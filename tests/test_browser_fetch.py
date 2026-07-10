"""M5 浏览器引擎单测（无需真浏览器/PG）：用假 playwright 注入 sys.modules 验证 browser 分支 + 能力探测。

真实浏览器冒烟（起 chromium 渲染真页）需 `pyp-agent[browser]` extra + `playwright install chromium`，
在有浏览器运行时的机器（agent 镜像/Linux）上做——本单测只证「分支选择 + 渲染取内容 + 能力上报」接线正确。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from payipa_contracts import EngineHint
from pyp_agent import fetch as fetch_mod


def _install_fake_playwright(html: str, status: int, final_url: str) -> types.ModuleType:
    """构造假 playwright.async_api，goto→content 返回给定 HTML；返回装好的模块（供清理）。"""
    calls: dict = {}

    class _Resp:
        def __init__(self) -> None:
            self.status = status

    class _Page:
        url = final_url

        async def goto(self, url, wait_until=None, timeout=None):
            calls["goto"] = {"url": url, "wait_until": wait_until, "timeout": timeout}
            return _Resp()

        async def content(self):
            return html

    class _Context:
        def __init__(self, **kw) -> None:
            calls["context_kwargs"] = kw

        async def new_page(self):
            return _Page()

    class _Browser:
        async def new_context(self, **kw):
            return _Context(**kw)

        async def close(self):
            calls["closed"] = True

    class _Chromium:
        async def launch(self, headless=True):
            calls["headless"] = headless
            return _Browser()

    class _PW:
        chromium = _Chromium()

    class _PWCtx:
        async def __aenter__(self):
            return _PW()

        async def __aexit__(self, *exc):
            return False

    def async_playwright():
        return _PWCtx()

    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = async_playwright
    pkg.async_api = api
    sys.modules["playwright"] = pkg
    sys.modules["playwright.async_api"] = api
    fetch_mod._fake_calls = calls  # 挂上供断言
    return pkg


def _uninstall_fake() -> None:
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.async_api", None)
    if hasattr(fetch_mod, "_fake_calls"):
        del fetch_mod._fake_calls


def test_browser_unavailable_raises() -> None:
    """没装 playwright → browser_available False，browser 引擎明确报错（分组派发规避）。"""
    _uninstall_fake()
    assert fetch_mod.browser_available() is False
    with pytest.raises(NotImplementedError, match="playwright"):
        asyncio.run(fetch_mod.fetch("http://x", engine_hint=EngineHint.BROWSER))


def test_browser_fetch_renders_html() -> None:
    """装了（假）playwright → browser_available True，fetch(browser) 起 headless chromium 渲染取 HTML。"""
    _install_fake_playwright("<html><body>rendered</body></html>", status=200, final_url="http://x/final")
    try:
        assert fetch_mod.browser_available() is True
        result = asyncio.run(
            fetch_mod.fetch("http://x", engine_hint=EngineHint.BROWSER, timeout=5.0, headers={"user-agent": "pyp"})
        )
        assert result.status == 200
        assert result.url == "http://x/final"
        assert b"rendered" in result.body
        assert result.content_type.startswith("text/html")
        # 接线断言：headless 起、DOM 就绪等待、超时换算 ms、UA header 注入 context、浏览器关闭
        calls = fetch_mod._fake_calls
        assert calls["headless"] is True
        assert calls["goto"]["wait_until"] == "domcontentloaded"
        assert calls["goto"]["timeout"] == 5000
        assert calls["context_kwargs"]["extra_http_headers"] == {"user-agent": "pyp"}
        assert calls["closed"] is True
    finally:
        _uninstall_fake()


def test_http_engine_unaffected(monkeypatch) -> None:
    """http 引擎不碰 playwright（browser_available 与它无关）。"""
    _uninstall_fake()

    async def fake_http(url: str, timeout: float, headers: dict[str, str] | None) -> fetch_mod.FetchResult:
        return fetch_mod.FetchResult(status=200, url=url, body=b"ok", content_type="text/plain")

    monkeypatch.setattr(fetch_mod, "_fetch_http", fake_http)
    result = asyncio.run(fetch_mod.fetch("https://example.test/", engine_hint=EngineHint.HTTP, timeout=0.2))
    assert result.status == 200
    assert result.body == b"ok"
