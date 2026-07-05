"""`business` 组装产物库：M0 无固定表。

`asm_{assembly短码}` 产物表是**运行时动态表**（同 data_* 混合 schema），由组装装载时程序化建表，
**不进 Alembic**（M3 落地）。此模块仅保证 BusinessBase 元数据被注册（空基线）。
"""

from __future__ import annotations

from payipa.db.base import BusinessBase

__all__ = ["BusinessBase"]
