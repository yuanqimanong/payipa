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
from payipa.security.tokens import verify_upload_token
from payipa.storage import get_storage, record_artifact
from payipa.studio.gateway import QueryGateway
from payipa_contracts import ArtifactRef, RulePack, TableQueryRequest

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/rules/{content_hash}", response_model=RulePack, summary="agent 按内容 hash 拉规则（内容寻址）")
async def get_rule(content_hash: str) -> RulePack:
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
    if not claims or claims.get("s") != source_uuid or str(claims.get("b")) != str(batch_id):
        raise HTTPException(status_code=401, detail="invalid or mismatched upload token")

    storage = get_storage()
    if not storage.disk_ok():
        raise HTTPException(status_code=503, detail="disk watermark low; upload rejected")

    data = await request.body()
    ref = await storage.save_raw(source_uuid, batch_id, url, data, content_type=content_type)
    expires_at = datetime.now(UTC) + timedelta(days=settings.raw_retention_days)  # raw 保留期 → GC
    await record_artifact(ref, task_id=task_id, agent_id=agent_id, source_id=source_uuid, expires_at=expires_at)
    return ref


@router.post("/query", summary="Query Gateway：沙箱经此结构化取数（job_token 鉴权 + scope 授权，红线2）")
async def query_gateway(
    req: TableQueryRequest,
    x_job_token: str = Header(..., description="组装作业令牌（JWT；scope=可读表 + 行数配额）"),
) -> dict:
    """用户/AI 代码读数的**唯一**受控入口：验 job_token（签名+有效期）→ 查 scope 是否含 data_{source} →
    结构化 SELECT（无 SQL 串）→ 回 {rows, next_cursor, quota}。越权表 403、令牌无效 401。M3 首版 JSON 传输，
    Arrow IPC 与只读 PG 角色在后续硬化切片。"""
    claims = decode_job_token(get_settings().upload_secret, x_job_token)
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired job token")
    table = data_table_name(req.source)
    if not token_allows_table(claims, table):
        raise HTTPException(status_code=403, detail=f"job token scope does not allow table {table}")
    rows, cursor, quota = await QueryGateway().read(get_engine("data_center"), req)
    return {
        "rows": rows,
        "next_cursor": cursor.model_dump() if cursor else None,
        "quota": quota.model_dump(),
    }
