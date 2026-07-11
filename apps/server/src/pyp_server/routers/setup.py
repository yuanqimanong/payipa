"""首次启动引导（P0-05 页面化）：系统未初始化（users 表为空）时引导创建首个管理员。

占位：实现随本批 UX 交付填充。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["setup"])
