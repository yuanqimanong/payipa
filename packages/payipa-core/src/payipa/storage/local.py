"""local 兜底后端：主控数据目录（未配对象存储时）。数据集中可查可备份，绝不落子节点。"""

from __future__ import annotations

import shutil
from pathlib import Path

from payipa_contracts import ArtifactRef
from payipa_contracts import StorageBackend as BackendKind

from payipa.storage.base import StorageBackend


class LocalBackend(StorageBackend):
    kind = BackendKind.LOCAL
    bucket = "local"

    def __init__(self, data_root: str | Path, min_free_bytes: int = 500 * 1024 * 1024) -> None:
        self.root = Path(data_root)
        self.min_free_bytes = min_free_bytes

    def _path(self, object_key: str) -> Path:
        # 防路径穿越：object_key 由服务端 key 方案生成，仍做一次归一化校验
        p = (self.root / object_key).resolve()
        root = self.root.resolve()
        if root not in p.parents and p != root:
            raise ValueError(f"非法 object_key（越界）: {object_key}")
        return p

    def disk_ok(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free >= self.min_free_bytes

    async def save_bytes(self, object_key: str, data: bytes, content_type: str | None = None) -> ArtifactRef:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._ref(self.bucket, self.kind, object_key, data, content_type)

    async def get_bytes(self, object_key: str) -> bytes:
        return self._path(object_key).read_bytes()

    async def delete(self, object_key: str) -> None:
        self._path(object_key).unlink(missing_ok=True)
