"""pyp-agent 单元测试：CLI 解析、无 core 依赖（红线）、WS url 推导、process_task 全流程（monkeypatch）、响应上限。"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from pathlib import Path

import payipa_contracts as c
import pytest
from pyp_agent import conn as conn_mod
from pyp_agent import fetch as fetch_mod
from pyp_agent.fetch import FetchNetworkError, FetchResult, FetchTimeout, FetchTooLarge
from pyp_agent.url_policy import URLPolicyError

FIXTURE = Path(__file__).parent / "fixtures" / "books_list.html"


@pytest.fixture(autouse=True)
def _public_test_dns(monkeypatch):
    async def public_dns(_host: str, _port: int) -> set[str]:
        return {"93.184.216.34"}

    monkeypatch.setattr("pyp_agent.url_policy.resolve_host", public_dns)


def test_cli_parser_join() -> None:
    from pyp_agent.cli import build_parser

    args = build_parser().parse_args(["join", "--server", "https://x", "--token", "t", "--slots", "8"])
    assert args.command == "join"
    assert args.server == "https://x"
    assert args.slots == 8


def test_agent_does_not_import_core() -> None:
    # 全新解释器只导入 agent 各模块，断言未拉入 payipa（core）
    code = (
        "import pyp_agent.cli, pyp_agent.conn, pyp_agent.rules, "
        "pyp_agent.upload, pyp_agent.sandbox_client, sys; "
        "bad=[m for m in sys.modules if m=='payipa' or m.startswith('payipa.')]; "
        "assert not bad, bad; print('clean')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_ws_url_derivation() -> None:
    assert conn_mod._ws_url("https://pyp.example.com") == "wss://pyp.example.com/ws/agent"
    assert conn_mod._ws_url("http://127.0.0.1:8000/") == "ws://127.0.0.1:8000/ws/agent"


def _books_rule() -> c.RulePack:
    return c.RulePack(
        item_locator=c.Locator(type=c.LocatorType.CSS, expr="article.product_pod"),
        fields=[
            c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@title")),
            c.FieldRule(
                name="url",
                locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@href"),
                type=c.FieldType.STORE_LINK,
                clean=[c.CleanOp(op="url_normalize")],
            ),
        ],
        fingerprint=["title"],
    )


def test_process_task_full_flow(monkeypatch) -> None:
    """拉规则→fetch→解析→上传 raw→回 ResultReport（fetch/upload monkeypatch，无网络）。"""
    html = FIXTURE.read_bytes()
    rule = _books_rule()
    uploaded: dict = {}

    class FakeCache:
        async def get(self, ptr: c.RulePointer, token: str | None = None) -> c.RulePack:
            return rule

    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(status=200, url=url, body=html, content_type="text/html")

    async def fake_upload(server_base: str, token: str, **kw) -> c.ArtifactRef:
        uploaded.update(kw)
        return c.ArtifactRef(bucket="local", object_key="k.zst", backend=c.StorageBackend.LOCAL, size=len(html))

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    monkeypatch.setattr(conn_mod, "upload_raw_via_server", fake_upload)

    task = c.TaskSpec(
        task_id="t1",
        req_id="rq1",
        batch_id="b1",
        source="books",
        target="https://books.toscrape.com/",
        rule_ptr=c.RulePointer(rule_id="1", version=1, content_hash="h"),
        archive_raw=True,
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token="tok", server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert report.type == "result"
    assert report.result.req_id == "rq1"
    assert len(report.result.items) == 3
    assert report.result.summary.count_ok == 3
    assert report.result.summary.response_status == 200
    assert report.result.summary.response_bytes == len(html)
    assert report.result.summary.engine == "http"
    assert len(report.result.artifacts) == 1  # raw 上传回指针
    assert uploaded["source_uuid"] == "books"  # 上传带对了 source/batch


def test_process_task_applies_rule_failure_and_layout_policies(monkeypatch) -> None:
    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(status=200, url=url, body=b"<html>challenge page</html>", content_type="text/html")

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)

    class FailCache:
        async def get(self, _ptr, token=None):
            return c.RulePack(
                fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
                fail_when=c.FailWhen(body_contains=["challenge page"]),
            )

    task = c.TaskSpec(
        task_id="policy",
        req_id="fail-when",
        batch_id="b",
        source="books",
        target="https://example.test/page",
        rule_ptr=c.RulePointer(rule_id="1", version=1, content_hash="h"),
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FailCache(), agent_id="a")
    )
    assert isinstance(report, c.StatusReport)
    assert report.state == int(c.ErrorCode.SOFT_FAIL)
    assert report.reason_code == "rule_fail_when"

    class LayoutCache:
        async def get(self, _ptr, token=None):
            return c.RulePack(
                fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))],
                layout_match=c.LayoutMatch(url_regex=r"/detail/", body_regex=r"product-main"),
            )

    report = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=LayoutCache(), agent_id="a")
    )
    assert isinstance(report, c.StatusReport)
    assert report.state == int(c.ErrorCode.PARSE_FAIL)
    assert report.reason_code == "layout_mismatch"


def test_process_task_uses_final_url_and_never_archives_test_channel(monkeypatch) -> None:
    uploaded: list[dict] = []

    class FakeCache:
        async def get(self, _ptr, token=None):
            return c.RulePack(
                fields=[
                    c.FieldRule(
                        name="url",
                        locator=c.Locator(type=c.LocatorType.CSS, expr="a@href"),
                        type=c.FieldType.STORE_LINK,
                        clean=[c.CleanOp(op="url_normalize")],
                    )
                ]
            )

    async def fake_fetch(_url: str, **_kw) -> FetchResult:
        return FetchResult(
            status=200,
            url="https://example.test/redirected/index.html",
            body=b'<a href="detail/1">one</a>',
            content_type="text/html",
        )

    async def fake_upload(*_args, **kwargs):
        uploaded.append(kwargs)

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    monkeypatch.setattr(conn_mod, "upload_raw_via_server", fake_upload)
    task = c.TaskSpec(
        task_id="test-channel",
        req_id="r",
        batch_id="b",
        source="books",
        target="https://example.test/start",
        rule_ptr=c.RulePointer(rule_id="1", version=1, content_hash="h"),
        channel=c.Channel.TEST,
        archive_raw=True,
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token="token", server_base="http://x", rule_cache=FakeCache(), agent_id="a")
    )
    assert isinstance(report, c.ResultReport)
    assert report.result.items[0].fields["url"] == "https://example.test/redirected/detail/1"
    assert uploaded == []


def test_process_task_enforces_result_frame_budget(monkeypatch) -> None:
    class FakeCache:
        async def get(self, _ptr, token=None):
            return c.RulePack(fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"))])

    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(status=200, url=url, body=b"<h1>large result</h1>", content_type="text/html")

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    monkeypatch.setattr(conn_mod, "max_result_bytes", lambda: 32)
    task = c.TaskSpec(
        task_id="budget",
        req_id="r",
        batch_id="b",
        source="books",
        target="https://example.test/page",
        rule_ptr=c.RulePointer(rule_id="1", version=1, content_hash="h"),
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FakeCache(), agent_id="a")
    )
    assert isinstance(report, c.StatusReport)
    assert report.reason_code == "result_too_large"


def test_process_task_pauses_before_parse_or_archive(monkeypatch) -> None:
    """明确的访问拒绝必须在解析和 raw 归档前停止。"""
    called = {"upload": False}

    class FakeCache:
        async def get(self, ptr: c.RulePointer, token: str | None = None) -> c.RulePack:
            return _books_rule()

    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(status=403, url=url, body=b"not archived", content_type="text/html")

    async def fake_upload(*_args, **_kwargs):
        called["upload"] = True
        raise AssertionError("access-refused response must not be archived")

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    monkeypatch.setattr(conn_mod, "upload_raw_via_server", fake_upload)
    task = c.TaskSpec(
        task_id="t2",
        req_id="rq2",
        batch_id="b2",
        source="books",
        target="https://example.test/private",
        rule_ptr=c.RulePointer(rule_id="2", version=1, content_hash="h2"),
    )

    report = asyncio.run(
        conn_mod.process_task(task, upload_token="tok", server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )

    assert isinstance(report, c.StatusReport)
    assert report.state == int(c.ErrorCode.ACCESS_PAUSED)
    assert report.response_status == 403
    assert report.reason_code == "access_denied"
    assert called["upload"] is False


def test_process_task_honors_retry_after_before_parse_or_archive(monkeypatch) -> None:
    called = {"upload": False}

    class FakeCache:
        async def get(self, _ptr, token=None):
            return _books_rule()

    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(
            status=429,
            url=url,
            body=b"slow down",
            content_type="text/plain",
            headers={"retry-after": "42"},
        )

    async def fake_upload(*_args, **_kwargs):
        called["upload"] = True
        raise AssertionError("throttled response must not be archived")

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    monkeypatch.setattr(conn_mod, "upload_raw_via_server", fake_upload)
    task = c.TaskSpec(
        task_id="t3",
        req_id="rq3",
        batch_id="b3",
        source="books",
        target="https://example.test/limited",
        rule_ptr=c.RulePointer(rule_id="3", version=1, content_hash="h3"),
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token="tok", server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert isinstance(report, c.StatusReport)
    assert report.state == int(c.ErrorCode.THROTTLED)
    assert report.retry_after_s == 42
    assert report.reason_code == "rate_limited"
    assert called["upload"] is False


class _FakeResp:
    """niquests AsyncResponse 桩：headers 先到，iter_content 流式吐 body。"""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}
        self.status_code = 200
        self.url = "https://x.test/page"
        self.closed = False
        self.read_called = False

    async def iter_content(self, n: int):
        self.read_called = True
        body = self._body

        async def gen():
            for i in range(0, len(body), n):
                yield body[i : i + n]

        return gen()

    async def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, _url: str, **_kw) -> _FakeResp:
        return self._resp


def test_fetch_http_caps_body(monkeypatch) -> None:
    """流式读超限 → FetchTooLarge 并立即断开（防 Content-Length 缺失/说谎）。"""
    resp = _FakeResp(b"x" * 4096)
    monkeypatch.setattr(fetch_mod.niquests, "AsyncSession", lambda **_kwargs: _FakeSession(resp))
    with pytest.raises(FetchTooLarge):
        asyncio.run(fetch_mod.fetch("https://x.test/page", max_bytes=1024))
    assert resp.closed


def test_fetch_http_rejects_declared_oversize_without_reading(monkeypatch) -> None:
    """Content-Length 超限：头先拒，一个字节都不读。"""
    resp = _FakeResp(b"x" * 4096, headers={"content-length": "4096"})
    monkeypatch.setattr(fetch_mod.niquests, "AsyncSession", lambda **_kwargs: _FakeSession(resp))
    with pytest.raises(FetchTooLarge):
        asyncio.run(fetch_mod.fetch("https://x.test/page", max_bytes=1024))
    assert resp.closed
    assert not resp.read_called


def test_fetch_http_within_cap_streams_full_body(monkeypatch) -> None:
    body = b"y" * 2000
    resp = _FakeResp(body, headers={"content-length": "2000", "content-type": "text/html"})
    monkeypatch.setattr(fetch_mod.niquests, "AsyncSession", lambda **_kwargs: _FakeSession(resp))
    result = asyncio.run(fetch_mod.fetch("https://x.test/page", max_bytes=4096))
    assert result.body == body
    assert result.status == 200
    assert result.content_type == "text/html"


def test_url_policy_rejects_private_and_mixed_dns(monkeypatch) -> None:
    from pyp_agent.url_policy import validate_url

    async def private_dns(_host: str, _port: int) -> set[str]:
        return {"93.184.216.34", "127.0.0.1"}

    monkeypatch.setattr("pyp_agent.url_policy.resolve_host", private_dns)
    with pytest.raises(URLPolicyError, match="non-public"):
        asyncio.run(validate_url("https://allowed.test/page", ["allowed.test"]))


def test_url_policy_rejects_cross_domain_and_non_http() -> None:
    from pyp_agent.url_policy import validate_url

    with pytest.raises(URLPolicyError, match="allowlist"):
        asyncio.run(validate_url("https://other.test/page", ["allowed.test"]))
    with pytest.raises(URLPolicyError, match="http"):
        asyncio.run(validate_url("file:///etc/passwd", ["allowed.test"]))


def test_http_connector_resolver_rejects_rebound_private_address() -> None:
    from pyp_agent.url_policy import PublicAddressResolver

    class ReboundResolver:
        def is_available(self):
            return True

        async def close(self):
            return None

        async def getaddrinfo(self, host, port, family, type, proto=0, flags=0, **_kwargs):
            return [(family, type, proto, "", ("127.0.0.1", int(port)))]

    resolver = PublicAddressResolver(["allowed.test"], resolver=ReboundResolver())
    with pytest.raises(URLPolicyError, match="non-public"):
        asyncio.run(
            resolver.getaddrinfo(
                "allowed.test",
                443,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
        )


def test_max_body_bytes_env_knob(monkeypatch) -> None:
    monkeypatch.delenv("PYP_AGENT_MAX_BODY_MB", raising=False)
    assert fetch_mod.max_body_bytes() == 10 * 1024 * 1024  # 默认 10MB
    monkeypatch.setenv("PYP_AGENT_MAX_BODY_MB", "2")
    assert fetch_mod.max_body_bytes() == 2 * 1024 * 1024
    monkeypatch.setenv("PYP_AGENT_MAX_BODY_MB", "nope")  # 非法值回落默认
    assert fetch_mod.max_body_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("PYP_AGENT_MAX_BODY_MB", "0")  # 非正值回落默认
    assert fetch_mod.max_body_bytes() == 10 * 1024 * 1024


def test_process_task_maps_oversize_response(monkeypatch) -> None:
    """超限响应 → SOFT_FAIL + response_too_large（不重试），不进解析/归档。"""

    class FakeCache:
        async def get(self, _ptr, token=None):
            return _books_rule()

    async def too_large(*_args, **_kwargs):
        raise FetchTooLarge("response body exceeds 1024 bytes cap")

    monkeypatch.setattr(conn_mod, "fetch", too_large)
    task = c.TaskSpec(
        task_id="t5",
        req_id="rq5",
        batch_id="b5",
        source="books",
        target="https://example.test/huge",
        rule_ptr=c.RulePointer(rule_id="5", version=1, content_hash="h5"),
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert isinstance(report, c.StatusReport)
    assert report.state == int(c.ErrorCode.SOFT_FAIL)
    assert report.reason_code == "response_too_large"
    assert report.retry_after_s is None


def test_process_task_maps_transport_failures(monkeypatch) -> None:
    class FakeCache:
        async def get(self, _ptr, token=None):
            return _books_rule()

    task = c.TaskSpec(
        task_id="t4",
        req_id="rq4",
        batch_id="b4",
        source="books",
        target="https://example.test/unavailable",
        rule_ptr=c.RulePointer(rule_id="4", version=1, content_hash="h4"),
    )

    async def timeout(*_args, **_kwargs):
        raise FetchTimeout("timed out")

    monkeypatch.setattr(conn_mod, "fetch", timeout)
    timed = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert isinstance(timed, c.StatusReport) and timed.state == int(c.ErrorCode.TIMEOUT)

    async def network(*_args, **_kwargs):
        raise FetchNetworkError("HTTP transport failed (ConnectionError)")

    monkeypatch.setattr(conn_mod, "fetch", network)
    failed = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert isinstance(failed, c.StatusReport) and failed.state == int(c.ErrorCode.NETWORK)


def test_process_task_echoes_attempt(monkeypatch) -> None:
    """P0-10：结果回报回显 TaskSpec.attempt（主控 fencing 依据）。"""
    html = FIXTURE.read_bytes()

    class FakeCache:
        async def get(self, _ptr, token=None):
            return _books_rule()

    async def fake_fetch(url: str, **_kw) -> FetchResult:
        return FetchResult(status=200, url=url, body=html, content_type="text/html")

    monkeypatch.setattr(conn_mod, "fetch", fake_fetch)
    task = c.TaskSpec(
        task_id="t5",
        req_id="rq5",
        batch_id="b5",
        source="books",
        target="https://books.toscrape.com/",
        rule_ptr=c.RulePointer(rule_id="5", version=1, content_hash="h5"),
        attempt=2,
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token=None, server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert report.type == "result" and report.result.attempt == 2


def test_handle_task_acks_first(monkeypatch) -> None:
    """P0-10：收到 TaskAssign 先回 TaskAck（带代次），再执行并回报。"""
    import json

    sent: list[dict] = []

    class FakeWS:
        async def send(self, text: str) -> None:
            sent.append(json.loads(text))

    async def fake_process(task, **_kw):
        return c.StatusReport(req_id=task.req_id, state=int(c.ErrorCode.SOFT_FAIL), message="x", attempt=task.attempt)

    monkeypatch.setattr(conn_mod, "process_task", fake_process)
    ac = conn_mod.AgentConnection("http://x", "tok")
    assign = c.TaskAssign(
        task=c.TaskSpec(
            task_id="t6",
            req_id="rq6",
            batch_id="b6",
            source="books",
            target="https://x/",
            rule_ptr=c.RulePointer(rule_id="6", version=1, content_hash="h6"),
            attempt=1,
        )
    )
    asyncio.run(ac._handle_task(FakeWS(), assign))
    assert sent[0]["type"] == "task_ack" and sent[0]["req_id"] == "rq6" and sent[0]["attempt"] == 1
    assert sent[1]["type"] == "status" and sent[1]["attempt"] == 1


def test_agent_local_slot_limit_is_enforced(monkeypatch) -> None:
    """即使主控误超发，本地同时执行数也不得超过 slot_n。"""
    active = 0
    peak = 0

    class FakeWS:
        async def send(self, _text: str) -> None:
            pass

    async def fake_process(task, **_kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return c.StatusReport(req_id=task.req_id, state=int(c.ErrorCode.SOFT_FAIL), attempt=task.attempt)

    monkeypatch.setattr(conn_mod, "process_task", fake_process)
    ac = conn_mod.AgentConnection("http://x", "tok", slot_n=1)

    def assigned(req_id: str) -> c.TaskAssign:
        return c.TaskAssign(
            task=c.TaskSpec(
                task_id="t-slot",
                req_id=req_id,
                batch_id="b-slot",
                source="books",
                target="https://x.test/",
                rule_ptr=c.RulePointer(rule_id="1", version=1, content_hash="h"),
            )
        )

    async def run_both() -> None:
        await asyncio.gather(ac._handle_task(FakeWS(), assigned("r1")), ac._handle_task(FakeWS(), assigned("r2")))

    asyncio.run(run_both())
    assert peak == 1


def test_state_identity_persists(tmp_path) -> None:
    """P0-08：node_uuid 首启生成后稳定；save_state 合并写不丢字段。"""
    from pyp_agent import state

    a = state.node_id(tmp_path)
    assert a == state.node_id(tmp_path) and len(a) == 32
    state.save_state(tmp_path, node_token="tk1")
    st = state.load_state(tmp_path)
    assert st["node_token"] == "tk1" and st["node_uuid"] == a


def test_result_spool_survives_until_ack(tmp_path) -> None:
    from pyp_agent import spool

    report = c.ResultReport(
        result=c.ResultBatch(
            batch_id="b-spool",
            req_id="r-spool",
            attempt=3,
            items=[c.Item(fields={"title": "durable"})],
            summary=c.ExecSummary(elapsed_s=0.1, count_ok=1),
        )
    )
    spool.put(tmp_path, report)
    restored = spool.pending(tmp_path)
    assert len(restored) == 1
    assert restored[0].result.items[0].fields["title"] == "durable"
    assert spool.ack(tmp_path, "r-spool", 2) is False
    assert len(spool.pending(tmp_path)) == 1
    assert spool.ack(tmp_path, "r-spool", 3) is True
    assert spool.pending(tmp_path) == []
