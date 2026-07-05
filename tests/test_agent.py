"""pyp-agent 单元测试：CLI 解析、无 core 依赖（红线）、WS url 推导、process_task 全流程（monkeypatch）。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import payipa_contracts as c
from pyp_agent import conn as conn_mod
from pyp_agent.fetch import FetchResult

FIXTURE = Path(__file__).parent / "fixtures" / "books_list.html"


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
        async def get(self, ptr: c.RulePointer) -> c.RulePack:
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
    )
    report = asyncio.run(
        conn_mod.process_task(task, upload_token="tok", server_base="http://x", rule_cache=FakeCache(), agent_id="a1")
    )
    assert report.type == "result"
    assert report.result.req_id == "rq1"
    assert len(report.result.items) == 3
    assert report.result.summary.count_ok == 3
    assert len(report.result.artifacts) == 1  # raw 上传回指针
    assert uploaded["source_uuid"] == "books"  # 上传带对了 source/batch
