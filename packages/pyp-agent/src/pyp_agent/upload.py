"""大对象上传：s3 presigned 直传 / local 走主控接口——同一套代码（storage 抽象屏蔽后端）。

复用 jianbing_utils.s3（multipart/presigned）。agent 只做 HTTP PUT/POST，零 S3 密钥。
M1：local 兜底路径（POST 主控 /internal/upload，token 鉴权，不走 WS）。s3 直传路径 M5。
"""

from __future__ import annotations

import niquests
from jianbing_utils.s3 import PresignedTarget
from payipa_contracts import ArtifactRef


async def upload_raw_via_server(
    server_base: str,
    upload_token: str,
    *,
    source_uuid: str,
    batch_id: int | str,
    url: str,
    data: bytes,
    content_type: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    timeout: float = 60.0,
) -> ArtifactRef:
    """local 兜底：把 raw 字节 POST 到主控 /internal/upload，回传永久工件指针。"""
    params: dict[str, str] = {"source_uuid": source_uuid, "batch_id": str(batch_id), "url": url}
    if content_type:
        params["content_type"] = content_type
    if task_id:
        params["task_id"] = task_id
    if agent_id:
        params["agent_id"] = agent_id
    async with niquests.AsyncSession() as session:
        resp = await session.post(
            f"{server_base.rstrip('/')}/internal/upload",
            params=params,
            headers={"x-upload-token": upload_token},
            data=data,
            timeout=timeout,
        )
    resp.raise_for_status()
    return ArtifactRef.model_validate(resp.json())


async def upload_via_presigned(target: PresignedTarget, data: bytes) -> None:
    """s3 直传：按 presigned 目标 PUT（M5，jbutils.s3 分片编排）。"""
    raise NotImplementedError("M5：presigned 直传 S3（jbutils.s3 multipart）")
