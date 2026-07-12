"""单测（无需 PG）：查询/网关的 LIKE 过滤必须转义用户输入里的 % 和 _，否则搜索 '50%' 会命中任意串。"""

from __future__ import annotations

from payipa.explore import query as q
from payipa.studio import gateway as gw


def test_like_escape_neutralizes_wildcards() -> None:
    for esc in (q._like_escape, gw._like_escape):
        assert esc("50%") == "50\\%"
        assert esc("a_b") == "a\\_b"
        assert esc("100%_done") == "100\\%\\_done"
        # 反斜杠先转义，避免用户注入转义字符改变后续语义
        assert esc("a\\b") == "a\\\\b"
        assert esc("plain") == "plain"


def test_query_filter_applies_escape_clause() -> None:
    from sqlalchemy import Column, Integer, MetaData, String, Table, select

    t = Table("t", MetaData(), Column("id", Integer), Column("name", String))
    stmt = q._apply_filters(select(t.c.id), t, [{"field": "name", "value": "50%", "type": "like"}])
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "ESCAPE" in compiled  # 生成了 LIKE ... ESCAPE 子句
    assert "50\\%" in compiled  # 通配符已被转义为字面量
