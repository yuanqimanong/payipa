"""工件（大对象）元数据。注意：这里是**永久对象 key**，不含会失效的 presigned URL。

presigned URL 是一次性传输凭证、用完即弃、不落库；事后访问按 object_key 随用随签。
"""

from __future__ import annotations

from pydantic import BaseModel

from payipa_contracts._annotate import active, reserved
from payipa_contracts.enums import ArtifactStatus, StorageBackend


class ArtifactRef(BaseModel):
    """结果回传时携带的轻量工件指针（大对象直传后回指针，不过控制面）。"""

    bucket: str = active("对象存储桶名（local 后端为逻辑桶）")
    object_key: str = active("永久对象 key，如 results/{task_id}/{attempt}/{filename}")
    backend: StorageBackend = active("存储后端：s3 直传 / local 主控盘兜底")
    size: int = active("对象字节数", ge=0)
    sha256: str | None = active("内容 SHA256（完整性校验/去重）", default=None)
    content_type: str | None = active("MIME 类型", default=None)


class Artifact(BaseModel):
    """工件完整元数据（对应 data_center.artifacts 表形状；传输模型，非 ORM）。"""

    bucket: str = active("对象存储桶名")
    object_key: str = active("永久对象 key")
    backend: StorageBackend = active("存储后端 s3/local")
    size: int = active("字节数", ge=0)
    sha256: str | None = active("内容 SHA256", default=None)
    etag: str | None = active("对象存储返回的 ETag（上传完成/复核）", default=None)
    content_type: str | None = active("MIME 类型", default=None)
    status: ArtifactStatus = active("生命周期状态", default=ArtifactStatus.PENDING)
    # 排查回溯关联键
    task_id: str | None = active("关联任务 id（correlation）", default=None)
    attempt_id: str | None = active("关联尝试 id", default=None, since="M2")
    agent_id: str | None = active("上传节点 id", default=None, since="M2")
    source_id: str | None = active("数据源 id", default=None)
    # 资源归属与保留期
    owner_id: str | None = active("资源属主（应用层 owner 过滤）", default=None, since="M2")
    tenant_id: str | None = reserved("租户 id（RLS 预留，v1 不启用）", default=None)
    expires_at: float | None = reserved("保留期截止 epoch 秒（artifact GC）", default=None, since="M2")
