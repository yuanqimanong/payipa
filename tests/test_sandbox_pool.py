"""M3 slice-7 沙箱 worker 池测试。

1. worker runner 协议（常驻循环 + /out 文件框定）——纯 stdlib，无 Docker/PG 也跑（CI 覆盖）；
2. 真容器池（预热复用 + 并发 + 脚本错误后仍可用）——无 Linux 容器运行时自动跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_STUDIO = Path(__file__).parent.parent / "packages" / "payipa-core" / "src" / "payipa" / "studio"


class _StubGateway(BaseHTTPRequestHandler):
    """单页数据的 Query Gateway 桩：回一行 {title:<源>}，据请求的 source 区分。"""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = {"rows": [{"title": body.get("source", "?")}], "next_cursor": None}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_gateway():
    server = ThreadingHTTPServer(("", 0), _StubGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/internal/query"
    finally:
        server.shutdown()


_ASSEMBLE = (
    "async def assemble(ctx):\n"
    "    rows = await ctx.read_table('src', columns=['title'])\n"
    "    return [{'t': r['title'].upper()} for r in rows]\n"
)


def test_worker_runner_processes_multiple_specs(stub_gateway: str, tmp_path: Path) -> None:
    """常驻 worker：单进程按 stdin 连续处理两条 spec，各写自己的 /out 文件（复用同进程）。"""
    job = tmp_path / "job"
    out = tmp_path / "out"
    job.mkdir()
    out.mkdir()
    shutil.copy(_STUDIO / "_sandbox_child.py", job / "child.py")
    shutil.copy(_STUDIO / "_sandbox_worker.py", job / "worker.py")

    specs = [
        json.dumps({"source": _ASSEMBLE, "gateway_url": stub_gateway, "job_token": "t",
                    "out_path": str(out / "a.json")}),
        json.dumps({"source": _ASSEMBLE, "gateway_url": stub_gateway, "job_token": "t",
                    "out_path": str(out / "b.json")}),
    ]  # fmt: skip
    proc = subprocess.Popen(
        [sys.executable, "-u", str(job / "worker.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(job),
    )
    try:
        proc.stdin.write(("\n".join(specs) + "\n").encode())
        proc.stdin.flush()
        deadline = time.time() + 30
        while not ((out / "a.json").exists() and (out / "b.json").exists()):
            if time.time() > deadline:
                raise AssertionError("worker 未在 30s 内产出两个结果")
            time.sleep(0.05)
        ra = json.loads((out / "a.json").read_text(encoding="utf-8"))
        rb = json.loads((out / "b.json").read_text(encoding="utf-8"))
        assert ra["ok"] and ra["rows"] == [{"t": "SRC"}]
        assert rb["ok"] and rb["rows"] == [{"t": "SRC"}]
        assert proc.poll() is None  # 处理完仍常驻（未退出）
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


# ── 真容器池层（无 Linux 容器运行时自动跳过） ────────────────────────────────


@pytest.fixture(scope="session")
def require_sandbox() -> None:
    from payipa.studio.sandbox import sandbox_available

    if not sandbox_available():
        pytest.skip("no Linux container runtime; skipping sandbox pool test")


def test_pool_warm_reuse_and_concurrency(require_sandbox: None, stub_gateway: str) -> None:
    """池化：size=1 连跑两作业（温热复用，只建一个 worker）；size=2 并发三作业全成。"""
    import anyio
    from payipa.studio.sandbox import SandboxPool, ensure_sandbox_infra

    port = int(stub_gateway.rsplit(":", 1)[1].split("/")[0])

    async def reuse() -> list:
        await ensure_sandbox_infra(port)
        async with SandboxPool("secret", size=1, gateway_port=port, timeout_s=90.0) as pool:
            names_before = {w["name"] for w in pool._workers}
            r1, _ = await pool.submit(_ASSEMBLE, job_id="j1", sources=["src"])
            r2, _ = await pool.submit(_ASSEMBLE, job_id="j2", sources=["src"])
            names_after = {w["name"] for w in pool._workers}
            assert names_before == names_after  # 同一 worker 复用，未重建
            return [r1, r2]

    results = anyio.run(reuse)
    assert all(r == [{"t": "SRC"}] for r in results)

    async def concurrent() -> list:
        await ensure_sandbox_infra(port)
        async with SandboxPool("secret", size=2, gateway_port=port, timeout_s=90.0) as pool:

            async def one(jid):
                rows, _ = await pool.submit(_ASSEMBLE, job_id=jid, sources=["src"])
                return rows

            out = []
            async with anyio.create_task_group() as tg:

                async def run(jid):
                    out.append(await one(jid))

                for jid in ("c1", "c2", "c3"):
                    tg.start_soon(run, jid)
            return out

    conc = anyio.run(concurrent)
    assert len(conc) == 3 and all(r == [{"t": "SRC"}] for r in conc)


def test_pool_script_error_then_still_usable(require_sandbox: None, stub_gateway: str) -> None:
    """池化：脚本失败抛 SandboxScriptError，worker 归还后仍可继续处理下一作业。"""
    import anyio
    from payipa.studio.sandbox import SandboxPool, SandboxScriptError, ensure_sandbox_infra

    port = int(stub_gateway.rsplit(":", 1)[1].split("/")[0])

    async def main() -> list:
        await ensure_sandbox_infra(port)
        async with SandboxPool("secret", size=1, gateway_port=port, timeout_s=90.0) as pool:
            with pytest.raises(SandboxScriptError, match="RuntimeError: boom"):
                await pool.submit(
                    "async def assemble(ctx):\n    raise RuntimeError('boom')\n", job_id="bad", sources=["src"]
                )
            rows, _ = await pool.submit(_ASSEMBLE, job_id="ok", sources=["src"])  # 同一 worker 仍可用
            return rows

    assert anyio.run(main) == [{"t": "SRC"}]
