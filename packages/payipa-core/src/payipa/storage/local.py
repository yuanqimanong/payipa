"""local 兜底后端：主控数据目录（未配对象存储时）。数据集中可查可备份，绝不落子节点。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import anyio.to_thread
from payipa_contracts import ArtifactRef
from payipa_contracts import StorageBackend as BackendKind

from payipa.storage.base import StorageBackend


def _atomic_write(path: Path, data: bytes) -> None:
    """临时文件 + os.replace 原子落盘：崩溃不留半截对象（tmp 与目标同目录，保证同卷可 replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


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
        await anyio.to_thread.run_sync(_atomic_write, path, data)  # 阻塞 IO 下线程，不卡事件循环
        return self._ref(self.bucket, self.kind, object_key, data, content_type)

    async def get_bytes(self, object_key: str) -> bytes:
        return await anyio.to_thread.run_sync(self._path(object_key).read_bytes)

    async def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))
