"""P0-13 单元测试（纯单元，无需 PG）：动态表标识符统一校验（payipa.db.ident）。

覆盖合法/非法/边界/注入尝试，以及 data_*/asm_* 拼名入口已接上校验（进 DDL 前即抛）。
"""

from __future__ import annotations

import pytest
from payipa.crawl.ingest import build_data_table, data_table_name
from payipa.db.ident import MAX_CODE, MAX_FIELD, MAX_IDENT, check_code, check_field, check_ident
from payipa.studio.asm import asm_table_name, build_asm_table

# ── 合法：原样返回、绝不改写 ──────────────────────────────────────────────────
GOOD_CODES = ["a", "m1test", "src_01", "a_b_c", "a" * MAX_CODE]


@pytest.mark.parametrize("code", GOOD_CODES)
def test_check_code_ok(code: str) -> None:
    assert check_code(code) == code  # 只校验不改写（job_token 表白名单依赖原样匹配）


def test_check_field_and_ident_ok() -> None:
    assert check_field("title") == "title"
    assert check_field("a" * MAX_FIELD) == "a" * MAX_FIELD
    assert check_ident("asm_m3prod") == "asm_m3prod"
    assert check_ident("a" * MAX_IDENT) == "a" * MAX_IDENT


# ── 非法：空/大写/中文/符号/注入/pg_ 前缀/超长，全部 ValueError ────────────────
BAD_CODES = [
    "",  # 空串
    "Abc",  # 大写
    "ABC",
    "1abc",  # 数字开头
    "_abc",  # 下划线开头
    "a-b",  # 连字符
    "a b",  # 空格
    "商品",  # 中文
    "a商品",
    'a"; drop table--',  # 注入：双引号逃逸
    "a; DROP TABLE x",  # 注入：分号
    "a'||'b",  # 注入：单引号拼接（生成列表达式内插风险）
    "a\x00b",  # NUL
    "pg_x",  # PG 保留前缀
    "a" * (MAX_CODE + 1),  # 超长
]


@pytest.mark.parametrize("code", BAD_CODES)
def test_check_code_bad(code: str) -> None:
    with pytest.raises(ValueError):
        check_code(code)


def test_check_field_bad_boundaries() -> None:
    with pytest.raises(ValueError, match="超长"):
        check_field("a" * (MAX_FIELD + 1))  # 超长会把 ix_data_{短码}_idx_{字段} 顶破 PG 63 字节
    with pytest.raises(ValueError, match="非法"):
        check_field("Title")
    with pytest.raises(ValueError, match="不能为空"):
        check_field("")


def test_check_ident_bad() -> None:
    with pytest.raises(ValueError, match="超长"):
        check_ident("a" * (MAX_IDENT + 1))
    with pytest.raises(ValueError, match="pg_"):
        check_ident("pg_catalog")
    with pytest.raises(ValueError):
        check_ident(None)  # type: ignore[arg-type]  # 非字符串同样拒绝


# ── 拼名/建表入口已接上校验（所有 DDL/取数路径的收口点）────────────────────────
def test_table_name_entrances_guarded() -> None:
    assert data_table_name("m1test") == "data_m1test"
    assert asm_table_name("m3prod") == "asm_m3prod"
    with pytest.raises(ValueError, match="短码"):
        data_table_name('a"; drop table--')
    with pytest.raises(ValueError, match="短码"):
        asm_table_name("pg_x")


def test_build_table_field_guarded() -> None:
    with pytest.raises(ValueError, match="索引字段名"):
        build_data_table("okcode", indexed_fields=["bad-field"])
    with pytest.raises(ValueError, match="索引字段名"):
        build_asm_table("okcode", indexed_fields=["a" * (MAX_FIELD + 1)])
    # 合法字段照常建出生成列 + 索引
    table = build_data_table("okcode", indexed_fields=["title"])
    assert "idx_title" in table.c
