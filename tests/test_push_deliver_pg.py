"""M4 slice-3 集成测试（需 PG）：推送组件端到端 —— 登记/签名门 → outbox → 隔离子进程投递到本地下游。

覆盖：①签名+active 组件经 Consumer 一轮排空成功投递（下游真收到、状态转 sent）；②未签名(draft)组件被签名门
拒→退避重试(pending)、达上限转 dead；③目标域白名单越界→投递失败（下游未收到）；④KEK 信封凭证解密后注入。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from payipa.db.settings import get_settings
from payipa.deliver.component import PushComponentStore, make_component_deliverer
from payipa.deliver.outbox import enqueue_push, run_outbox_once
from payipa.security.secrets import encrypt_json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_SECRET = "push-sign-secret-at-least-32-bytes-long-xx"
_KEK = "push-kek-secret-at-least-32-bytes-long-xxx"

_COMPONENT = """
def push(ctx):
    n = 0
    for row in ctx.rows:
        resp = ctx.http.post(ctx.creds["url"], json=row, headers={"x-token": ctx.creds["token"]})
        resp.raise_for_status()
        n += 1
    return n
"""


class _Downstream(BaseHTTPRequestHandler):
    received: list[dict] = []
    tokens: list[str] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        _Downstream.received.append(json.loads(self.rfile.read(n).decode()))
        _Downstream.tokens.append(self.headers.get("x-token", ""))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):  # silence
        pass


def _serve() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), _Downstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_push_deliver_end_to_end(require_pg: None) -> None:
    _Downstream.received.clear()
    _Downstream.tokens.clear()
    srv, port = _serve()
    url = f"http://127.0.0.1:{port}/hook"

    async def scenario() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        biz = create_async_engine(get_settings().async_url("business"))
        store = PushComponentStore(pyp)
        deliverer = make_component_deliverer(pyp, biz, sign_secret=_SECRET, kek=_KEK)
        creds_ct = encrypt_json({"url": url, "token": "sekret"}, kek=_KEK)
        try:
            # ── ① active + 签名组件 → 一轮排空成功投递 ──────────────────────
            cid, _, _ = await store.put(
                name="ds-hook", code=_COMPONENT, allow_domains=["127.0.0.1"], target_creds=creds_ct
            )
            await store.publish(cid, _SECRET)  # active + 签名
            payload = json.dumps({"kind": "inline", "rows": [{"a": 1}, {"a": 2}]})
            assert await enqueue_push(pyp, component_id=cid, payload_ref=payload, idempotency_key="k1") == 1
            sent, failed = await run_outbox_once(pyp, deliverer, max_attempts=5)
            assert (sent, failed) == (1, 0), (sent, failed)
            assert len(_Downstream.received) == 2 and _Downstream.received[0] == {"a": 1}
            assert _Downstream.tokens == ["sekret", "sekret"]  # KEK 信封解密后注入子进程
            st1 = await _state(pyp, cid, "k1")
            assert st1 == "sent", st1

            # ── ② 未签名(draft)组件 → 签名门拒 → 退避重试 → 达上限 dead ────────
            cid2, _, _ = await store.put(name="ds-unsigned", code=_COMPONENT, allow_domains=["127.0.0.1"])
            # 不 publish：status=draft、无签名
            await enqueue_push(pyp, component_id=cid2, payload_ref=payload, idempotency_key="k2")
            got_dead = False
            for _ in range(6):  # max_attempts=2 → 第 2 次失败即 dead
                await _clear_backoff(pyp, "k2")  # 抹掉 next_retry_at 让下一轮立即重领
                await run_outbox_once(pyp, deliverer, max_attempts=2)
                if await _state(pyp, cid2, "k2") == "dead":
                    got_dead = True
                    break
            assert got_dead, await _state(pyp, cid2, "k2")

            # ── ③ 白名单越界 → 投递失败（下游未收到新请求）────────────────────
            before = len(_Downstream.received)
            cid3, _, _ = await store.put(
                name="ds-offwl", code=_COMPONENT, allow_domains=["example.com"], target_creds=creds_ct
            )
            await store.publish(cid3, _SECRET)
            await enqueue_push(pyp, component_id=cid3, payload_ref=payload, idempotency_key="k3")
            _, failed3 = await run_outbox_once(pyp, deliverer, max_attempts=5)
            assert failed3 == 1
            assert len(_Downstream.received) == before, "off-whitelist push must not reach downstream"
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM push_outbox WHERE idempotency_key IN ('k1','k2','k3')"))
                await conn.execute(
                    text("DELETE FROM push_components WHERE name IN ('ds-hook','ds-unsigned','ds-offwl')")
                )
            await pyp.dispose()
            await biz.dispose()

    asyncio.run(scenario())
    srv.shutdown()


async def _state(pyp, component_id: int, key: str) -> str:
    async with pyp.begin() as conn:
        return (
            await conn.execute(
                text("SELECT state FROM push_outbox WHERE component_id=:c AND idempotency_key=:k"),
                {"c": component_id, "k": key},
            )
        ).scalar_one()


async def _clear_backoff(pyp, key: str) -> None:
    async with pyp.begin() as conn:
        await conn.execute(
            text("UPDATE push_outbox SET next_retry_at=NULL WHERE idempotency_key=:k AND state='pending'"),
            {"k": key},
        )
