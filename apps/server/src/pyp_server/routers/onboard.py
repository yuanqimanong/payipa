"""首次使用向导（P0-21）：系统就绪 → 接入第一个采集节点 → 第一条示例采集 → 完成清单。

向导状态存服务端（global_params key=onboarding），换浏览器/重登录不丢；管理员可随时重开 /welcome。
示例采集用 books.toscrape.com（Zyte 提供的公开爬虫练习站，允许抓取），单页 20 条、保守限速。
"""

from __future__ import annotations

import logging

import payipa_contracts as c
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from payipa.db.engine import get_engine
from payipa.db.pyp import GlobalParam, Source
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pyp_server.auth import get_current_user, require_user
from pyp_server.service import dispatch_source_run

router = APIRouter(tags=["onboard"])
logger = logging.getLogger("pyp_server.onboard")

_KEY = "onboarding"  # global_params 键：{"done": bool}
DEMO_CODE = "demo_books"
_DEMO_SEED = "https://books.toscrape.com/"


def demo_rule() -> c.RulePack:
    """示例规则：列表页每本书取标题 + 详情链接（不跟进，单页演示）。"""
    return c.RulePack(
        item_locator=c.Locator(type=c.LocatorType.CSS, expr="article.product_pod"),
        fields=[
            c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@title"), index=True),
            c.FieldRule(
                name="url",
                locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@href"),
                clean=[c.CleanOp(op="url_normalize")],
            ),
        ],
        fingerprint=["title"],
    )


@router.get("/welcome", response_class=HTMLResponse, summary="首次使用向导", include_in_schema=False)
async def welcome_page(request: Request):
    user = await get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    host = request.headers.get("host") or "127.0.0.1:8000"
    return request.app.state.templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "user": user,
            "active": "",
            "server_url": f"{request.url.scheme}://{host}",
            "demo_code": DEMO_CODE,
        },
    )


async def _state() -> dict:
    pyp = get_engine("pyp")
    async with pyp.connect() as conn:
        row = (await conn.execute(select(GlobalParam.value).where(GlobalParam.key == _KEY))).scalar()
        demo = (await conn.execute(select(Source.id).where(Source.uuid == DEMO_CODE))).first()
    return {"done": bool((row or {}).get("done")), "demo_created": demo is not None}


@router.get("/api/onboard/state", summary="向导状态", dependencies=[Depends(require_user)])
async def state() -> dict:
    return await _state()


@router.post("/api/onboard/demo", summary="创建示例数据源并试跑一次", dependencies=[Depends(require_user)])
async def run_demo() -> dict:
    """幂等：源已存在则复用（dispatch_source_run 对既有源只更新参数并新建批次）。"""
    result = await dispatch_source_run(
        uuid=DEMO_CODE,
        name="示例：books.toscrape.com",
        seed_urls=[_DEMO_SEED],
        rule=demo_rule(),
        access_basis="public_policy",
        access_reference="Zyte 提供的公开爬虫练习站点（toscrape.com），明确允许抓取练习",
        access_confirmed=True,
        rate_limit=2,  # 演示站，保守限速
    )
    logger.info("onboarding demo source dispatched: batch=%s", result["batch_id"])
    return {"source": DEMO_CODE, "batch_id": result["batch_id"], "requests": result["requests"]}


@router.post("/api/onboard/done", summary="标记向导完成", dependencies=[Depends(require_user)])
async def mark_done() -> dict:
    async with get_engine("pyp").begin() as conn:
        await conn.execute(
            pg_insert(GlobalParam.__table__)
            .values(key=_KEY, value={"done": True})
            # upsert 不触发 onupdate，updated_at 须显式刷新（DB-014 约定）
            .on_conflict_do_update(index_elements=["key"], set_={"value": {"done": True}, "updated_at": func.now()})
        )
    return {"done": True}
