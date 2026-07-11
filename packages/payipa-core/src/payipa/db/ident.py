"""动态表标识符统一校验（P0-13）。

data_*/asm_* 是运行时动态表：分表短码、勾选「需索引」的字段名最终都会内插进 DDL
（CREATE TABLE 生成列表达式 / ALTER TABLE / CREATE INDEX / 索引名）。所有进 DDL 的
标识符必须先过本模块——规则：小写字母开头、只含 [a-z0-9_]、限长、拒绝 pg_ 前缀
（PostgreSQL 系统对象保留）。非法即抛 ValueError（中文信息），不落任何 DDL。
校验只判合法性、绝不改写输入（job_token 的表白名单等依赖原样匹配）。
"""

from __future__ import annotations

import re

MAX_IDENT = 63  # PG 标识符上限 63 字节，超出会被静默截断（截断还可能撞名）
MAX_CODE = 32  # 分表短码：对齐 sources.uuid / assemblies.product_code 的 String(32)
MAX_FIELD = 18  # 索引字段：保证最长索引名 ix_data_{32位短码}_idx_{字段} 不超 63

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def _check(name: object, max_len: int, kind: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind}不能为空")
    if len(name) > max_len:
        raise ValueError(f"{kind} {name!r} 超长：最多 {max_len} 个字符")
    if not _IDENT.match(name):
        raise ValueError(f"{kind} {name!r} 非法：须以小写字母开头，只能含小写字母、数字、下划线")
    if name.startswith("pg_"):
        raise ValueError(f"{kind} {name!r} 非法：pg_ 是 PostgreSQL 保留前缀")
    return name


def check_ident(name: str) -> str:
    """通用 DDL 标识符（表名/列名/索引名）：合法原样返回，非法抛 ValueError。"""
    return _check(name, MAX_IDENT, "标识符")


def check_code(code: str) -> str:
    """分表短码（data_{code} / asm_{code} 的 code 段：数据源短码 / 产物短码）。"""
    return _check(code, MAX_CODE, "短码")


def check_field(name: str) -> str:
    """勾选「需索引」的字段名（会拼进 idx_<字段> 生成列与 ix_* 索引名）。"""
    return _check(name, MAX_FIELD, "索引字段名")


__all__ = ["MAX_CODE", "MAX_FIELD", "MAX_IDENT", "check_code", "check_field", "check_ident"]
