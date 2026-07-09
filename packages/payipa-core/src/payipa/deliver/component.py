"""推送组件登记/签名门 + outbox 投递器工厂（M4 slice-3c）。

推送组件 = 第三类脚本（05 §1.2），复用脚本基建：内容寻址（code + 白名单 + 名）= content_hash；状态机
draft→testing→active；**发布即签名**（HMAC content_hash，红线7）。执行前签名门校验，只有 active/testing 且
签名验过者可跑。投递器把组件丢隔离子进程（:mod:`payipa.deliver.pushexec`），只注入解密后的目标凭证 + 白名单。

payload_ref 载荷解析（三触发统一走 outbox，05 §4-3）：JSON spec —— ``{"kind":"inline","rows":[...]}`` 或
``{"kind":"dataset","product_code":..,"after_id":0,"limit":1000}``（读 business 库 asm_ 产物）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payipa.db.pyp import PushComponent
from payipa.deliver.dataset import read_dataset
from payipa.deliver.outbox import Deliverer
from payipa.deliver.pushexec import run_push_component
from payipa.security.secrets import decrypt_json

_COLS = (
    PushComponent.id,
    PushComponent.status,
    PushComponent.content_hash,
    PushComponent.signature,
    PushComponent.code,
    PushComponent.allow_domains,
    PushComponent.target_creds,
    PushComponent.version,
)


def component_content_hash(*, code: str, allow_domains: Sequence[str], name: str) -> str:
    """组件内容寻址：源码 + 目标域白名单（排序）+ 名 → sha256（版本 pin）。"""
    blob = json.dumps(
        {"code": code, "allow_domains": sorted(allow_domains), "name": name},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def sign_component(secret: str, content_hash: str) -> str:
    return hmac.new(secret.encode(), content_hash.encode(), hashlib.sha256).hexdigest()


def verify_component_signature(secret: str, content_hash: str, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(signature, sign_component(secret, content_hash))


def assert_component_runnable(comp: Row, secret: str) -> None:
    """执行器签名门：仅 active/testing 且签名验过的组件可投递；否则 PermissionError（红线7）。"""
    if comp.status not in ("active", "testing"):
        raise PermissionError(f"push component {comp.id} status={comp.status} not runnable (need active/testing)")
    if not verify_component_signature(secret, comp.content_hash or "", comp.signature):
        raise PermissionError(f"push component {comp.id} signature invalid or missing")


class PushComponentStore:
    """推送组件登记（content_hash 去重、按 name 递增版本）+ 状态机 + 发布签名。"""

    def __init__(self, engine_pyp: AsyncEngine) -> None:
        self.engine = engine_pyp

    async def put(
        self,
        *,
        name: str,
        code: str,
        allow_domains: Sequence[str] = (),
        target_creds: str | None = None,
    ) -> tuple[int, str, int]:
        """登记组件（content_hash 去重）；返回 (id, content_hash, version)。新内容 status=draft。"""
        digest = component_content_hash(code=code, allow_domains=allow_domains, name=name)
        async with self.engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(PushComponent.id, PushComponent.version).where(PushComponent.content_hash == digest)
                )
            ).first()
            if existing:
                return int(existing[0]), digest, int(existing[1])
            max_v = (
                await conn.execute(select(func.max(PushComponent.version)).where(PushComponent.name == name))
            ).scalar()
            version = (max_v or 0) + 1
            cid = (
                await conn.execute(
                    pg_insert(PushComponent.__table__)
                    .values(
                        name=name,
                        code=code,
                        allow_domains=list(allow_domains),
                        target_creds=target_creds,
                        content_hash=digest,
                        status="draft",
                        version=version,
                    )
                    .returning(PushComponent.id)
                )
            ).scalar_one()
        return int(cid), digest, version

    async def set_status(self, component_id: int, status: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(PushComponent.__table__).where(PushComponent.id == component_id).values(status=status)
            )

    async def publish(self, component_id: int, secret: str) -> str:
        """发布：status=active + 写签名（HMAC content_hash）。返回签名。"""
        async with self.engine.begin() as conn:
            ch = (
                await conn.execute(select(PushComponent.content_hash).where(PushComponent.id == component_id))
            ).scalar_one()
            sig = sign_component(secret, ch)
            await conn.execute(
                update(PushComponent.__table__)
                .where(PushComponent.id == component_id)
                .values(status="active", signature=sig)
            )
        return sig

    async def get(self, component_id: int) -> Row | None:
        async with self.engine.begin() as conn:
            return (await conn.execute(select(*_COLS).where(PushComponent.id == component_id))).first()


async def _resolve_payload(engine_business: AsyncEngine, payload_ref: str | None) -> list[dict]:
    """解析 outbox.payload_ref（JSON spec）为待推送行。inline 直取；dataset 读 asm_ 产物；空/异形 → []。"""
    if not payload_ref:
        return []
    try:
        spec = json.loads(payload_ref)
    except json.JSONDecodeError, TypeError:
        return []
    if not isinstance(spec, dict):
        return []
    kind = spec.get("kind")
    if kind == "inline":
        rows = spec.get("rows") or []
        return [r for r in rows if isinstance(r, dict)]
    if kind == "dataset":
        rows, _ = await read_dataset(
            engine_business,
            str(spec["product_code"]),
            after_id=int(spec.get("after_id", 0)),
            limit=int(spec.get("limit", 1000)),
        )
        return rows
    return []


def make_component_deliverer(
    engine_pyp: AsyncEngine,
    engine_business: AsyncEngine,
    *,
    sign_secret: str,
    kek: str | None = None,
) -> Deliverer:
    """构造 outbox 投递器：加载组件（签名门）→ 解密目标凭证 → 解析载荷 → 隔离子进程投递。

    投递失败（组件缺失/未签名/子进程报错）抛异常，由 outbox 状态机决定退避/死信。成功即返回。
    """

    async def deliver(outbox_row: dict) -> None:
        comp = await PushComponentStore(engine_pyp).get(int(outbox_row["component_id"]))
        if comp is None:
            raise RuntimeError(f"push component {outbox_row['component_id']} not found")
        assert_component_runnable(comp, sign_secret)  # 红线7：未签名/非 active → PermissionError
        if not comp.code:
            raise RuntimeError(f"push component {comp.id} has no code")
        creds = decrypt_json(comp.target_creds, kek=kek) if comp.target_creds else {}
        rows = await _resolve_payload(engine_business, outbox_row.get("payload_ref"))
        result = await run_push_component(comp.code, rows, creds=creds, allow_domains=list(comp.allow_domains or []))
        if not result.ok:
            raise RuntimeError(result.error or "push component failed")

    return deliver


DeliverFn = Callable[[dict], Awaitable[None]]

__all__ = [
    "PushComponentStore",
    "assert_component_runnable",
    "component_content_hash",
    "make_component_deliverer",
    "sign_component",
    "verify_component_signature",
]
