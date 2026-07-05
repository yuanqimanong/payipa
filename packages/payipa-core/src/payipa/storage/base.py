"""存储后端抽象：StorageBackend → S3Backend(M5) / LocalBackend(M1 兜底)。

raw（网页/JSON）经 zstd 压缩归档；agent 上传代码同一套（后端差异由抽象屏蔽）。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from compression import zstd

from payipa_contracts import ArtifactRef
from payipa_contracts import StorageBackend as BackendKind

from payipa.storage.keys import raw_object_key

DEFAULT_ZSTD_LEVEL = 12  # 倾向 9~12（02 §2.4；最终档 POC 定）


class StorageBackend(ABC):
    """存储后端抽象。子类实现字节级读写与磁盘水位。"""

    kind: BackendKind
    bucket: str

    @abstractmethod
    async def save_bytes(self, object_key: str, data: bytes, content_type: str | None = None) -> ArtifactRef: ...

    @abstractmethod
    async def get_bytes(self, object_key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, object_key: str) -> None:
        """删除对象（GC / artifact 清理用）。不存在应静默。"""

    @abstractmethod
    def disk_ok(self) -> bool:
        """磁盘水位是否充足（低于阈值应拒绝新上传）。对象存储恒 True。"""

    async def save_raw(
        self,
        source_uuid: str,
        batch_id: int | str,
        url: str,
        data: bytes,
        *,
        content_type: str | None = None,
        level: int = DEFAULT_ZSTD_LEVEL,
    ) -> ArtifactRef:
        """raw 归档：zstd 压缩后按 key 方案落库，返回工件指针。"""
        key = raw_object_key(source_uuid, batch_id, url)
        compressed = zstd.compress(data, level=level)
        return await self.save_bytes(key, compressed, content_type=content_type or "application/zstd")

    async def get_raw(self, object_key: str) -> bytes:
        """取回并解压 raw。"""
        return zstd.decompress(await self.get_bytes(object_key))

    @staticmethod
    def _ref(bucket: str, kind: BackendKind, object_key: str, data: bytes, content_type: str | None) -> ArtifactRef:
        return ArtifactRef(
            bucket=bucket,
            object_key=object_key,
            backend=kind,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )
