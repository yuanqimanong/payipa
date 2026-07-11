"""M3 slice-6 黄金端到端（需 PG + Linux 容器运行时，二者缺一自动跳过）：

data_* → 真 uvicorn 主控（/internal/query，job_token + 签名游标 + 配额）→ egress 代理（仅该路径）
→ 锁定容器执行组装源码 → 行回传 → 可信父进程幂等装载 asm_*。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
from payipa.crawl.ingest import build_data_table, create_data_table, drop_data_table
from payipa.db.settings import get_settings
from payipa.studio.asm import build_asm_table, drop_asm_table
from payipa.studio.run import run_assembly_sandboxed
from payipa.studio.sandbox import SandboxExecutor, ensure_sandbox_infra, sandbox_available
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "sbxsrc"
_PROD = "sbxprod"


@pytest.fixture(scope="module")
def require_sandbox() -> None:
    if not sandbox_available():
        pytest.skip("no Linux container runtime; skipping sandbox E2E")


@pytest.fixture
def live_server():
    """真 uvicorn（绑 0.0.0.0 供容器经 host.docker.internal 回访）；测试后关停。"""
    import uvicorn
    from pyp_server.main import app

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            pytest.fail("uvicorn 未在 15s 内就绪")
        time.sleep(0.1)
    try:
        yield port
    finally:
        # 确定性关停：等线程真正退出（不放弃为守护线程），避免残留 uvicorn 事件循环/asyncpg 连接
        # 与后续测试共享的 get_engine 缓存跨事件循环干扰（曾致 test_sched_cancel_pg 偶发脏读/FK）。
        server.should_exit = True
        for _ in range(200):  # 最多等 20s
            t.join(timeout=0.1)
            if not t.is_alive():
                break
        assert not t.is_alive(), "uvicorn 线程未能在 20s 内关停"


def test_sandboxed_assembly_end_to_end(require_pg: None, require_sandbox: None, live_server: int) -> None:
    port = live_server

    async def main() -> None:
        dc = create_async_engine(get_settings().async_url("data_center"))
        biz = create_async_engine(get_settings().async_url("business"))
        src = build_data_table(_UUID, [])
        asm = build_asm_table(_PROD, [])
        try:
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await create_data_table(dc, src)
            async with dc.begin() as conn:
                for i, t in enumerate(["alpha", "beta", "gamma"]):
                    await conn.execute(pg_insert(src).values(data_fingerprint=f"fp{i}", state=3, fields={"title": t}))

            await ensure_sandbox_infra(port)
            sandbox = SandboxExecutor(get_settings().upload_secret, gateway_port=port, timeout_s=120.0)
            written = await run_assembly_sandboxed(
                biz,
                product_code=_PROD,
                script_source=(
                    "async def assemble(ctx):\n"
                    f"    rows = await ctx.read_table('{_UUID}', limit=2)\n"  # limit=2 → 强制走签名游标翻页
                    "    return [{'upper': r['fields']['title'].upper()} for r in rows]\n"
                ),
                sources=[_UUID],
                sandbox=sandbox,
                fingerprint_keys=["upper"],
                row_quota=100,
            )
            assert written == 3
            async with biz.connect() as conn:
                cnt = (await conn.execute(select(func.count()).select_from(asm))).scalar()
                vals = (await conn.execute(select(asm.c["fields"]))).scalars().all()
            assert cnt == 3
            assert {v["upper"] for v in vals} == {"ALPHA", "BETA", "GAMMA"}
        finally:
            await drop_data_table(dc, src)
            await drop_asm_table(biz, asm)
            await dc.dispose()
            await biz.dispose()

    asyncio.run(main())
