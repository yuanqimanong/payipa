"""M3 slice-6 沙箱执行器测试。

分三层：
1. runner 协议（_sandbox_child 子进程 + 本地 stub 网关）——纯 stdlib，无 Docker/PG 也跑（CI 覆盖）；
2. egress 代理 nginx 配置白名单——纯字符串单测；
3. 真容器（SandboxExecutor 起锁定容器经 egress 代理取数）——无 Linux 容器运行时自动跳过。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CHILD = Path(__file__).parent.parent / "packages" / "payipa-core" / "src" / "payipa" / "studio" / "_sandbox_child.py"


class _StubGateway(BaseHTTPRequestHandler):
    """两页数据的 Query Gateway 桩：首页带 next_cursor，第二页收尾；记录收到的请求供断言。"""

    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 —— BaseHTTPRequestHandler 约定
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"body": body, "token": self.headers.get("X-Job-Token")})
        if self.path != "/internal/query":
            self.send_response(403)
            self.end_headers()
            return
        if body.get("cursor_token"):
            payload = {"rows": [{"title": "b2", "id": 12}], "next_cursor": None}
        else:
            payload = {"rows": [{"title": "b1", "id": 11}], "next_cursor": "cur-1"}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture
def stub_gateway():
    _StubGateway.seen = []
    # 绑 0.0.0.0：容器经 host.docker.internal 回宿主时，原生 Linux docker 走 bridge IP 而非环回
    server = ThreadingHTTPServer(("", 0), _StubGateway)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/internal/query"
    finally:
        server.shutdown()


def _run_child(spec: dict, tmp_path: Path) -> dict:
    out_path = tmp_path / "result.json"
    spec = {**spec, "out_path": str(out_path)}
    proc = subprocess.run(
        [sys.executable, str(CHILD)], input=json.dumps(spec).encode(), capture_output=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_child_paginates_and_tracks_watermark(stub_gateway: str, tmp_path: Path) -> None:
    """runner：签名游标翻页合并两页；增量借 id 追水位并剥掉；X-Job-Token 随行。"""
    source = (
        "async def assemble(ctx):\n"
        "    rows = await ctx.read_table('books', columns=['title'], incremental=True)\n"
        "    return [{'t': r['title'].upper()} for r in rows]\n"
    )
    result = _run_child(
        {"source": source, "gateway_url": stub_gateway, "job_token": "tok-1", "watermarks": {"books": 5}},
        tmp_path,
    )
    assert result["ok"], result
    assert result["rows"] == [{"t": "B1"}, {"t": "B2"}]
    assert result["new_watermarks"] == {"books": 12}
    assert [c["token"] for c in _StubGateway.seen] == ["tok-1", "tok-1"]
    first, second = (c["body"] for c in _StubGateway.seen)
    assert {"column": "id", "op": "gt", "value": 5} in first["filters"]  # 增量=水位过滤
    assert "id" in first["columns"]  # 借 id 追水位
    assert second["cursor_token"] == "cur-1"  # 第二页带回签名游标


def test_child_reports_script_error(stub_gateway: str, tmp_path: Path) -> None:
    """runner：脚本异常编码回 {ok:False,error}，不炸进程。"""
    result = _run_child(
        {
            "source": "async def assemble(ctx):\n    raise ValueError('boom')\n",
            "gateway_url": stub_gateway,
            "job_token": "t",
        },
        tmp_path,
    )
    assert result["ok"] is False
    assert "ValueError: boom" in result["error"]


def test_child_rejects_missing_entry(stub_gateway: str, tmp_path: Path) -> None:
    result = _run_child({"source": "x = 1\n", "gateway_url": stub_gateway, "job_token": "t"}, tmp_path)
    assert result["ok"] is False
    assert "assemble" in result["error"]


def test_gateway_proxy_conf_whitelists_single_path() -> None:
    """egress 代理配置：仅放行 /internal/query，其余 403，目标端口注入正确。"""
    from payipa.studio.sandbox import gateway_proxy_conf

    conf = gateway_proxy_conf(8137)
    assert "location = /internal/query" in conf
    assert "host.docker.internal:8137" in conf
    assert "location / { return 403; }" in conf
    assert conf.count("proxy_pass") == 1  # 只此一个放行


def test_lockdown_workdir_no_world_write(tmp_path) -> None:
    """POSIX 上 job/out 目录不得世界可写（防共享 /tmp 竞态注入 result.json）；返回合法 --user 实参。"""
    import os

    from payipa.studio.sandbox import SandboxExecutor

    workdir, jobdir, outdir = tmp_path / "w", tmp_path / "w" / "job", tmp_path / "w" / "out"
    jobdir.mkdir(parents=True)
    outdir.mkdir()
    (jobdir / "child.py").write_text("x=1\n")
    sbx = SandboxExecutor("secret")
    user_arg = sbx._lock_down_workdir(workdir, jobdir, outdir)
    assert user_arg.count(":") == 1 and all(p.isdigit() for p in user_arg.split(":"))
    if hasattr(os, "getuid"):
        assert (outdir.stat().st_mode & 0o077) == 0, "outdir 不应对 group/other 开放"
        assert (jobdir.stat().st_mode & 0o077) == 0


def test_sandbox_runtime_and_ulimit_flags() -> None:
    """可选 gVisor 运行时与 fsize 上限落在构造参数上（不需容器即可核对）。"""
    from payipa.studio.sandbox import SandboxExecutor

    sbx = SandboxExecutor("secret", runtime="runsc", max_output_bytes=1234)
    assert sbx._runtime == "runsc"
    assert sbx._max_output_bytes == 1234
    assert SandboxExecutor("secret")._runtime is None  # 默认走 runc


# ── 真容器层（无 Linux 容器运行时自动跳过） ──────────────────────────────────


@pytest.fixture(scope="session")
def require_sandbox() -> None:
    from payipa.studio.sandbox import sandbox_available

    if not sandbox_available():
        pytest.skip("no Linux container runtime; skipping sandbox container test")


def test_sandbox_container_runs_script_via_egress_proxy(require_sandbox: None, stub_gateway: str) -> None:
    """真容器端到端：锁定容器 → egress 代理（仅 /internal/query）→ 宿主 stub 网关 → 行回传。

    stub 网关代替主控进程（协议同 /internal/query），无需 PG——专测容器/网络/协议链路。
    """
    import anyio
    from payipa.studio.sandbox import SandboxExecutor, ensure_sandbox_infra

    port = int(stub_gateway.rsplit(":", 1)[1].split("/")[0])

    async def main() -> tuple[list[dict], dict[str, int]]:
        await ensure_sandbox_infra(port)
        sbx = SandboxExecutor("test-secret", gateway_port=port, timeout_s=90.0)
        return await sbx.run_source(
            "async def assemble(ctx):\n"
            "    rows = await ctx.read_table('books', columns=['title'])\n"
            "    return [{'t': r['title']} for r in rows]\n",
            job_id="sbx-e2e",
            sources=["books"],
        )

    rows, _wm = anyio.run(main)
    assert rows == [{"t": "b1"}, {"t": "b2"}]


def test_sandbox_egress_lockdown(require_sandbox: None, stub_gateway: str) -> None:
    """安全性质对抗验证：白名单外路径被代理 403；外网无路由（internal 网络）。"""
    import anyio
    from payipa.studio.sandbox import SandboxExecutor, ensure_sandbox_infra

    port = int(stub_gateway.rsplit(":", 1)[1].split("/")[0])
    source = (
        "import socket, urllib.request, urllib.error\n"
        "async def assemble(ctx):\n"
        "    out = {}\n"
        "    try:\n"
        "        urllib.request.urlopen('http://payipa-gw/api/data', timeout=10)\n"
        "        out['path_escape'] = 'ALLOWED'\n"
        "    except urllib.error.HTTPError as exc:\n"
        "        out['path_escape'] = f'HTTP {exc.code}'\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 80), timeout=3).close()\n"
        "        out['direct_net'] = 'ALLOWED'\n"
        "    except OSError:\n"
        "        out['direct_net'] = 'blocked'\n"
        "    return [out]\n"
    )

    async def main() -> list[dict]:
        await ensure_sandbox_infra(port)
        sbx = SandboxExecutor("test-secret", gateway_port=port, timeout_s=90.0)
        rows, _ = await sbx.run_source(source, job_id="sbx-lockdown", sources=["books"])
        return rows

    (row,) = anyio.run(main)
    assert row["path_escape"] == "HTTP 403", row  # 代理只放行 /internal/query
    assert row["direct_net"] == "blocked", row  # internal 网络无外网路由


def test_sandbox_script_error_surfaces(require_sandbox: None, stub_gateway: str) -> None:
    import anyio
    from payipa.studio.sandbox import SandboxExecutor, SandboxScriptError, ensure_sandbox_infra

    port = int(stub_gateway.rsplit(":", 1)[1].split("/")[0])

    async def main() -> None:
        await ensure_sandbox_infra(port)
        sbx = SandboxExecutor("test-secret", gateway_port=port, timeout_s=90.0)
        with pytest.raises(SandboxScriptError, match="RuntimeError: nope"):
            await sbx.run_source(
                "async def assemble(ctx):\n    raise RuntimeError('nope')\n",
                job_id="sbx-err",
                sources=["books"],
            )

    anyio.run(main)
