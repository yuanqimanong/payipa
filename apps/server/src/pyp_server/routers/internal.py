"""内部端点（非公网，sidecar/agent ↔ 主控，token 鉴权）。

M1：`/internal/upload` —— local 兜底时 agent 经此流式回传 raw（不走 WS）。主控计算 object_key、
zstd 压缩、存 local、登记 artifacts，回传永久指针。S3 后端时 agent 改直传 presigned（M5）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, Request
from payipa.crawl.ingest import data_table_name
from payipa.crawl.rules import RuleStore
from payipa.db.engine import get_engine
from payipa.db.settings import get_settings
from payipa.security.job_token import decode_job_token, token_allows_table
from payipa.security.tokens import verify_rule_token, verify_upload_token
from payipa.storage import get_storage, record_artifact
from payipa.studio.cursor import decode_cursor, encode_cursor
from payipa.studio.gateway import QueryGateway
from payipa_contracts import ArtifactRef, KeysetCursor, QuotaMeta, RulePack, TableQueryRequest

from pyp_server.settings import get_server_settings

router = APIRouter(prefix="/internal", tags=["internal"])


async def _read_body(request: Request, limit: int) -> bytes:
    """流式读请求体并强制上限：超限即 413 停读（防 Content-Length 缺失/说谎把内存拖爆）。"""
    declared = request.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"body exceeds upload limit ({limit} bytes)")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"body exceeds upload limit ({limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/rules/{content_hash}", response_model=RulePack, summary="agent 按内容 hash 拉规则（内容寻址）")
async def get_rule(
    content_hash: str,
    x_rule_token: str = Header(..., description="绑定 content_hash 的短期规则读取 token"),
) -> RulePack:
    if not verify_rule_token(get_settings().upload_secret, x_rule_token, content_hash):
        raise HTTPException(status_code=401, detail="invalid or expired rule token")
    pack = await RuleStore(get_engine("pyp")).get_by_hash(content_hash)
    if pack is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return pack


@router.post("/upload", response_model=ArtifactRef, summary="local 兜底：回传 raw（token 鉴权，不走 WS）")
async def upload_raw(
    request: Request,
    source_uuid: str = Query(..., description="数据源短码"),
    batch_id: int = Query(..., description="批次 id"),
    url: str = Query(..., description="原始抓取 URL（用于计算 object_key）"),
    content_type: str | None = Query(None),
    task_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    x_upload_token: str = Header(..., description="内部上传一次性 token（绑定 source+batch）"),
) -> ArtifactRef:
    settings = get_settings()
    claims = verify_upload_token(settings.upload_secret, x_upload_token)
    if (
        not claims
        or claims.get("s") != source_uuid
        or str(claims.get("b")) != str(batch_id)
        or claims.get("c") != "prod"
    ):
        raise HTTPException(status_code=401, detail="invalid or mismatched upload token")

    storage = get_storage()
    if not storage.disk_ok():
        raise HTTPException(status_code=503, detail="disk watermark low; upload rejected")

    data = await _read_body(request, get_server_settings().max_upload_mb * 1024 * 1024)
    ref = await storage.save_raw(source_uuid, batch_id, url, data, content_type=content_type)
    expires_at = datetime.now(UTC) + timedelta(days=settings.raw_retention_days)  # raw 保留期 → GC
    await record_artifact(ref, task_id=task_id, agent_id=agent_id, source_id=source_uuid, expires_at=expires_at)
    return ref


@router.post("/query", summary="Query Gateway：沙箱经此结构化取数（job_token 鉴权 + scope 授权 + 行数配额，红线2）")
async def query_gateway(
    req: TableQueryRequest,
    x_job_token: str = Header(..., description="组装作业令牌（JWT；scope=可读表 + 行数配额）"),
) -> dict:
    """用户/AI 代码读数的**唯一**受控入口：验 job_token（签名+有效期）→ scope 授权（表白名单，越权 403）
    → 配额强制（scope.row_quota；已消费行数编码进签名游标累计，耗尽 403）→ 结构化 SELECT（无 SQL 串）
    → 回 {rows, next_cursor(签名不透明), quota}。伪造/跨作业游标 400。JSON 传输；Arrow/只读角色留硬化切片。"""
    secret = get_settings().upload_secret
    claims = decode_job_token(secret, x_job_token)
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired job token")
    try:  # 短码统一校验（P0-13，data_table_name 内置）：非法 400，不进 scope 匹配/查询
        table = data_table_name(req.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not token_allows_table(claims, table):
        raise HTTPException(status_code=403, detail=f"job token scope does not allow table {table}")

    jti = str(claims.get("jti"))
    quota_limit = (claims.get("scope") or {}).get("row_quota")
    after_id, consumed = 0, 0
    if req.cursor_token:  # 翻页：验签 + jti/source 绑定；伪造/篡改/跨作业 → 400
        cur = decode_cursor(secret, req.cursor_token, jti=jti, source=req.source)
        if cur is None:
            raise HTTPException(status_code=400, detail="invalid cursor")
        after_id, consumed = cur["a"], cur["c"]

    limit = req.limit
    if quota_limit is not None:
        remaining = int(quota_limit) - consumed
        if remaining <= 0:
            raise HTTPException(status_code=403, detail="row quota exhausted")
        limit = min(limit, remaining)

    inner = TableQueryRequest(
        source=req.source, columns=req.columns, filters=req.filters, limit=limit, cursor=KeysetCursor(after_id=after_id)
    )
    rows, cursor, _ = await QueryGateway().read(get_engine("data_center"), inner)
    consumed += len(rows)
    remaining_after = (int(quota_limit) - consumed) if quota_limit is not None else None
    next_token = None
    if cursor is not None and (remaining_after is None or remaining_after > 0):
        next_token = encode_cursor(secret, after_id=cursor.after_id, consumed=consumed, jti=jti, source=req.source)
    quota = QuotaMeta(rows_returned=len(rows), quota=quota_limit, rows_remaining=remaining_after)
    return {"rows": rows, "next_cursor": next_token, "quota": quota.model_dump()}
