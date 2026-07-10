"""M4 slice-5 集成测试（需 PG）：批次收尾自动触发（链路自动推送 + 收尾通知）+ 手动推送/通知端点。"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import payipa_contracts as c
from fastapi.testclient import TestClient
from payipa.crawl import run
from payipa.db.engine import get_engine
from payipa.db.pyp import Batch, PushComponent, Request, Source, Task
from payipa.db.settings import get_settings
from payipa.deliver.notify import NotifyBotStore
from pyp_server.main import app
from pyp_server.triggers import on_batch_finalized
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "m4trig"


class _Sink(BaseHTTPRequestHandler):
    got: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        _Sink.got.append(json.loads(self.rfile.read(n).decode()))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _make_source_task(conn, params: dict) -> tuple[int, int]:
    src_id = (
        await conn.execute(
            pg_insert(Source.__table__)
            .values(
                uuid=_UUID,
                name="M4 Trig",
                connector_type="web",
                access_basis="owned",
                access_reference="test fixture",
                access_confirmed_at=func.now(),
            )
            .returning(Source.id)
        )
    ).scalar_one()
    task_id = (
        await conn.execute(
            pg_insert(Task.__table__).values(source_id=src_id, trigger_type="manual", params=params).returning(Task.id)
        )
    ).scalar_one()
    return int(src_id), int(task_id)


def test_batch_finalize_auto_triggers(require_pg: None) -> None:
    _Sink.got.clear()
    srv, port = _serve()
    hook = f"http://127.0.0.1:{port}/lark"

    async def main() -> None:
        pyp = get_engine("pyp")  # 与 on_batch_finalized 同引擎/同事件循环
        store = NotifyBotStore(pyp)
        try:
            # 推送组件（draft 即可，本例只验证入队不验证投递）+ 通知机器人（config 用默认 KEK 加密）
            async with pyp.begin() as conn:
                pc_id = (
                    await conn.execute(
                        pg_insert(PushComponent.__table__)
                        .values(name="trig-pc", code="def push(ctx):\n    return 0", status="draft", version=1)
                        .returning(PushComponent.id)
                    )
                ).scalar_one()
            bot_id = await store.create(name="trig-bot", type="lark", config={"webhook": hook})

            async with pyp.begin() as conn:
                _, task_id = await _make_source_task(
                    conn,
                    {"notify_bot_id": bot_id, "push_component_id": int(pc_id), "product_code": "trigds"},
                )
                batch_id = (
                    await conn.execute(
                        pg_insert(Batch.__table__).values(task_id=task_id, status="running").returning(Batch.id)
                    )
                ).scalar_one()
                await conn.execute(
                    pg_insert(Request.__table__).values(
                        batch_id=batch_id, target="https://x/a", state=int(c.RequestState.SUCCESS)
                    )
                )

            # 无未完成请求 → 收尾成功（唯一那次）→ 触发钩子
            assert await run.finalize_batch_if_done(pyp, int(batch_id)) is True
            await on_batch_finalized(int(batch_id))

            # ① 链路自动推送：outbox 落一条 batch-<id>（pending，绑定该组件）
            async with pyp.begin() as conn:
                ob = (
                    await conn.execute(
                        text("SELECT component_id, state, payload_ref FROM push_outbox WHERE idempotency_key=:k"),
                        {"k": f"batch-{batch_id}"},
                    )
                ).first()
            assert ob is not None and ob.component_id == pc_id and ob.state == "pending"
            assert json.loads(ob.payload_ref) == {"kind": "dataset", "product_code": "trigds"}

            # ② 收尾通知：机器人收到 "批次 <id> done" 文本
            assert _Sink.got, "notify bot should have received a message"
            assert f"批次 {batch_id} done" in _Sink.got[-1]["content"]["text"]
            assert "成功 1/1" in _Sink.got[-1]["content"]["text"]
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM push_outbox WHERE idempotency_key=:k"), {"k": f"batch-{batch_id}"})
                for sql in (
                    "DELETE FROM requests WHERE batch_id IN (SELECT b.id FROM batches b JOIN tasks t ON b.task_id=t.id JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM batches WHERE task_id IN (SELECT t.id FROM tasks t JOIN sources s ON t.source_id=s.id WHERE s.uuid=:u)",  # noqa: E501
                    "DELETE FROM tasks WHERE source_id IN (SELECT id FROM sources WHERE uuid=:u)",
                    "DELETE FROM sources WHERE uuid=:u",
                    "DELETE FROM push_components WHERE name='trig-pc'",
                    "DELETE FROM notify_bots WHERE name='trig-bot'",
                ):
                    await conn.execute(text(sql), {"u": _UUID})

    asyncio.run(main())
    srv.shutdown()


def test_manual_push_and_notify_endpoints(require_pg: None) -> None:
    _Sink.got.clear()
    srv, port = _serve()
    hook = f"http://127.0.0.1:{port}/lark"

    # 用独立引擎建组件/机器人并 dispose，避免与 TestClient 事件循环串引擎
    async def seed() -> tuple[int, int]:
        eng = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with eng.begin() as conn:
                pc_id = (
                    await conn.execute(
                        pg_insert(PushComponent.__table__)
                        .values(name="man-pc", code="def push(ctx):\n    return 0", status="draft", version=1)
                        .returning(PushComponent.id)
                    )
                ).scalar_one()
            bot_id = await NotifyBotStore(eng).create(name="man-bot", type="lark", config={"webhook": hook})
            return int(pc_id), int(bot_id)
        finally:
            await eng.dispose()

    async def cleanup() -> None:
        eng = create_async_engine(get_settings().async_url("pyp"))
        try:
            async with eng.begin() as conn:
                await conn.execute(text("DELETE FROM push_outbox WHERE idempotency_key='man-k1'"))
                await conn.execute(text("DELETE FROM push_components WHERE name='man-pc'"))
                await conn.execute(text("DELETE FROM notify_bots WHERE name='man-bot'"))
        finally:
            await eng.dispose()

    pc_id, bot_id = asyncio.run(seed())
    try:
        with TestClient(app) as client:
            # 手动推送：内联行入 outbox
            r = client.post(
                f"/api/push/components/{pc_id}/enqueue",
                json={"rows": [{"a": 1}], "idempotency_key": "man-k1"},
            )
            assert r.status_code == 200 and r.json()["enqueued"] == 1, r.text
            # 幂等：同键再入 → 0
            r2 = client.post(
                f"/api/push/components/{pc_id}/enqueue",
                json={"rows": [{"a": 1}], "idempotency_key": "man-k1"},
            )
            assert r2.json()["enqueued"] == 0

            # 手动通知测试：机器人收到
            rn = client.post(f"/api/notify/{bot_id}/test", json={"title": "Ping", "text": "hi"})
            assert rn.status_code == 200 and rn.json() == {"ok": True}, rn.text
            assert _Sink.got[-1]["content"]["text"] == "Ping\nhi"

            # 未知机器人 → 400
            assert client.post("/api/notify/9999999/test", json={}).status_code == 400
    finally:
        asyncio.run(cleanup())
        srv.shutdown()
