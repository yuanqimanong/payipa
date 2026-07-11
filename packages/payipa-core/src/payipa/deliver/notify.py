"""用户级通知机器人（M4 slice-4，05 §1.1-6 / §4-6）：lark / 通用 webhook / email 轻量通知渠道。

区别于推送组件（代码类出口、隔离子进程）：通知渠道是**内置轻量渠道**——用户自配 webhook/收件人、绑定任务
通知进度/状态，**无需写代码**，故在主控可信进程内直接发（httpx / smtplib）。config（webhook URL / SMTP 凭证）
按红线9 用 KEK 信封加密存储（用户级自管、平台代管密钥），发送前解密。

支持 type：``lark``（群机器人 webhook，发 text 消息）/ ``webhook``（通用 POST JSON）/ ``email``（SMTP）。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import anyio
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import NotifyBot
from payipa.security.secrets import decrypt_json, encrypt_json


class NotifyError(RuntimeError):
    """通知发送失败（下游非 2xx / SMTP 报错 / 配置缺字段）。"""


class NotifyBotStore:
    """通知机器人登记：config 以 KEK 信封加密存储（用户级自管）。"""

    def __init__(self, engine_pyp: AsyncEngine) -> None:
        self.engine = engine_pyp

    async def create(
        self, *, name: str, type: str, config: dict, owner_id: int | None = None, kek: str | None = None
    ) -> int:
        """登记一个通知机器人；config 加密入库。返回 id。"""
        async with self.engine.begin() as conn:
            return int(
                (
                    await conn.execute(
                        NotifyBot.__table__.insert()
                        .values(name=name, type=type, config=encrypt_json(config, kek=kek), owner_id=owner_id)
                        .returning(NotifyBot.id)
                    )
                ).scalar_one()
            )

    async def get_config(self, bot_id: int, *, kek: str | None = None) -> tuple[str, dict] | None:
        """取机器人 (type, 解密后的 config)；不存在返回 None。"""
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(NotifyBot.type, NotifyBot.config).where(NotifyBot.id == bot_id))).first()
        if row is None:
            return None
        cfg = decrypt_json(row.config, kek=kek) if row.config else {}
        return row.type, cfg

    async def delete(self, bot_id: int) -> bool:
        """删除通知机器人；返回是否确有一行被删。"""
        async with self.engine.begin() as conn:
            result = await conn.execute(NotifyBot.__table__.delete().where(NotifyBot.id == bot_id))
        return bool(result.rowcount)


async def _post_json(url: str, payload: dict, *, timeout: float = 15.0) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.post(url, json=payload)


async def send_lark(config: dict, *, title: str, text: str) -> None:
    """飞书群机器人：POST webhook 发 text 消息（title 拼进正文首行）。"""
    url = config.get("webhook")
    if not url:
        raise NotifyError("lark bot config missing 'webhook'")
    body = f"{title}\n{text}" if title else text
    resp = await _post_json(url, {"msg_type": "text", "content": {"text": body}})
    if resp.status_code // 100 != 2:
        raise NotifyError(f"lark webhook returned {resp.status_code}: {resp.text[:200]}")


async def send_webhook(config: dict, *, title: str, text: str) -> None:
    """通用 webhook：POST {title, text} JSON（额外字段来自 config['extra']）。"""
    url = config.get("webhook") or config.get("url")
    if not url:
        raise NotifyError("webhook config missing 'webhook'/'url'")
    payload: dict[str, Any] = {"title": title, "text": text, **(config.get("extra") or {})}
    resp = await _post_json(url, payload)
    if resp.status_code // 100 != 2:
        raise NotifyError(f"webhook returned {resp.status_code}: {resp.text[:200]}")


def _build_email(config: dict, *, title: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = title or "(payipa notification)"
    msg["From"] = config["from"]
    to = config["to"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg.set_content(text)
    return msg


def _smtp_send(config: dict, msg: EmailMessage) -> None:
    host, port = config["host"], int(config.get("port", 587))
    use_tls = config.get("tls", True)
    with smtplib.SMTP(host, port, timeout=config.get("timeout", 20)) as smtp:
        if use_tls:
            smtp.starttls()
        if config.get("username"):
            smtp.login(config["username"], config.get("password", ""))
        smtp.send_message(msg)


async def send_email(config: dict, *, title: str, text: str) -> None:
    """SMTP 邮件：必填 host/from/to；可选 port/tls/username/password。同步 smtplib 丢线程池跑。"""
    for field in ("host", "from", "to"):
        if not config.get(field):
            raise NotifyError(f"email config missing {field!r}")
    msg = _build_email(config, title=title, text=text)
    try:
        await anyio.to_thread.run_sync(_smtp_send, config, msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"smtp send failed: {exc}") from exc


_SENDERS = {"lark": send_lark, "webhook": send_webhook, "email": send_email}


async def notify(engine_pyp: AsyncEngine, bot_id: int, *, title: str, text: str, kek: str | None = None) -> None:
    """向指定机器人发一条通知：加载 + 解密 config → 按 type 分发。未知/缺失 → NotifyError。"""
    got = await NotifyBotStore(engine_pyp).get_config(bot_id, kek=kek)
    if got is None:
        raise NotifyError(f"notify bot {bot_id} not found")
    bot_type, config = got
    sender = _SENDERS.get(bot_type)
    if sender is None:
        raise NotifyError(f"unsupported notify bot type: {bot_type!r}")
    await sender(config, title=title, text=text)


__all__ = ["NotifyBotStore", "NotifyError", "notify", "send_email", "send_lark", "send_webhook"]
