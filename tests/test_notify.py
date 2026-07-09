"""M4 slice-4 测试：用户级通知机器人 —— lark/webhook/email 渠道 + KEK 加密 config + notify 分发。

test_notify_channels 不需 PG（直接调渠道发到本地 HTTP 下游）——CI 也跑；test_notify_store_and_dispatch 需 PG。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from payipa.deliver.notify import (
    NotifyBotStore,
    NotifyError,
    _build_email,
    notify,
    send_lark,
    send_webhook,
)

_KEK = "notify-kek-secret-at-least-32-bytes-long-x"


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


def test_notify_channels() -> None:
    _Sink.got.clear()
    srv, port = _serve()
    url = f"http://127.0.0.1:{port}/hook"

    async def run() -> None:
        # lark: text 消息，title 拼进正文首行
        await send_lark({"webhook": url}, title="Batch 42 done", text="ok=20 fail=0")
        assert _Sink.got[-1]["msg_type"] == "text"
        assert _Sink.got[-1]["content"]["text"] == "Batch 42 done\nok=20 fail=0"
        # 通用 webhook：POST {title, text} + extra
        await send_webhook({"url": url, "extra": {"env": "prod"}}, title="T", text="B")
        assert _Sink.got[-1] == {"title": "T", "text": "B", "env": "prod"}
        # 缺字段 → NotifyError（不发出）
        with pytest.raises(NotifyError):
            await send_lark({}, title="x", text="y")

    asyncio.run(run())
    srv.shutdown()

    # email：构造 MIME（不实际发），多收件人拼 To
    msg = _build_email({"from": "a@x.io", "to": ["b@y.io", "c@y.io"]}, title="Sub", text="Body")
    assert msg["Subject"] == "Sub" and msg["To"] == "b@y.io, c@y.io"
    assert msg.get_content().strip() == "Body"


def test_notify_store_and_dispatch(require_pg: None) -> None:
    from payipa.db.settings import get_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    _Sink.got.clear()
    srv, port = _serve()
    url = f"http://127.0.0.1:{port}/lark"

    async def run() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        store = NotifyBotStore(pyp)
        try:
            # config（webhook）加密入库；notify 加载→解密→发到本地下游
            bot_id = await store.create(name="notify-t", type="lark", config={"webhook": url}, kek=_KEK)
            # 库里存的是密文，不是明文 URL
            async with pyp.begin() as conn:
                raw = (
                    await conn.execute(text("SELECT config FROM notify_bots WHERE id=:i"), {"i": bot_id})
                ).scalar_one()
            assert url not in raw  # 加密存储（红线9）

            await notify(pyp, bot_id, title="Done", text="all good", kek=_KEK)
            assert _Sink.got[-1]["content"]["text"] == "Done\nall good"

            with pytest.raises(NotifyError):  # 未知机器人
                await notify(pyp, 9_999_999, title="x", text="y", kek=_KEK)
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM notify_bots WHERE name='notify-t'"))
            await pyp.dispose()

    asyncio.run(run())
    srv.shutdown()
