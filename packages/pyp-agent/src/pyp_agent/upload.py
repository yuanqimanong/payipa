"""大对象上传：s3 presigned 直传 / local 走主控接口——同一套代码（storage 抽象屏蔽后端）。

复用 jianbing_utils.s3（multipart/presigned/断点续传）。agent 只做 HTTP PUT，零 S3 密钥。
M0 骨架：实现于 M1。
"""

from __future__ import annotations

from pathlib import Path

from jianbing_utils.s3 import PresignedTarget
from payipa_contracts import ArtifactRef


async def upload(path: Path, target: PresignedTarget) -> ArtifactRef:
    """把本地大对象上传到 storage 抽象给出的目标（s3 presigned 或 local 端点），回传指针。"""
    raise NotImplementedError("M1：按 target 分片 PUT（jbutils.s3），完成后回传 ArtifactRef")
