"""M1-3 单元测试：声明式解释器（列表页/JSON/清洗算子/字段级降级）。确定性、CI 友好。"""

from __future__ import annotations

from pathlib import Path

import payipa_contracts as c
from pyp_agent.interpret import interpret_page

FIXTURE = Path(__file__).parent / "fixtures" / "books_list.html"


def _list_rule() -> c.RulePack:
    return c.RulePack(
        item_locator=c.Locator(type=c.LocatorType.CSS, expr="article.product_pod"),
        fields=[
            c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@title")),
            c.FieldRule(
                name="url",
                locator=c.Locator(type=c.LocatorType.CSS, expr="h3 a@href"),
                type=c.FieldType.STORE_LINK,
                clean=[c.CleanOp(op="url_normalize")],
            ),
            c.FieldRule(
                name="price",
                locator=c.Locator(type=c.LocatorType.CSS, expr="p.price_color"),
                clean=[c.CleanOp(op="regex_extract", params={"pattern": r"([\d.]+)"})],
            ),
        ],
        fingerprint=["title"],
    )


def test_interpret_list_page() -> None:
    result = interpret_page(
        _list_rule(),
        FIXTURE.read_bytes(),
        url="https://books.toscrape.com/index.html",
        content_type="text/html",
    )
    assert len(result.items) == 3
    assert [i.fields["title"] for i in result.items] == ["Book A", "Book B", "Book C"]
    assert result.items[0].fields["price"] == "51.77"  # regex_extract 去掉 £
    # url 归一化为绝对地址 + 收集为新链接（M2 follow）
    assert result.items[0].fields["url"].startswith("https://books.toscrape.com/catalogue/book-a")
    assert len(result.links) == 3
    # FieldMeta 证据链
    fm = result.items[0].field_meta["title"]
    assert fm.normalized_value == "Book A"
    assert fm.confidence == 1.0
    assert fm.locator == "h3 a@title"


def test_interpret_json() -> None:
    rule = c.RulePack(
        item_locator=c.Locator(type=c.LocatorType.JSONPATH, expr="$.results[*]"),
        fields=[
            c.FieldRule(name="name", locator=c.Locator(type=c.LocatorType.JSONPATH, expr="$.name")),
            c.FieldRule(name="id", locator=c.Locator(type=c.LocatorType.JSONPATH, expr="$.id")),
        ],
        fingerprint=["id"],
    )
    body = b'{"results":[{"id":1,"name":"X"},{"id":2,"name":"Y"}]}'
    result = interpret_page(rule, body, url="https://api.example.com", content_type="application/json")
    assert len(result.items) == 2
    assert result.items[0].fields == {"name": "X", "id": 1}


def test_clean_parse_date_and_field_degradation() -> None:
    rule = c.RulePack(
        fields=[
            c.FieldRule(
                name="published_at",
                locator=c.Locator(type=c.LocatorType.CSS, expr="time@datetime"),
                clean=[c.CleanOp(op="parse_date")],
            ),
            c.FieldRule(
                name="missing",
                locator=c.Locator(type=c.LocatorType.CSS, expr=".does-not-exist"),
                clean=[c.CleanOp(op="default", params={"value": "N/A"})],
            ),
        ],
    )
    body = b'<html><body><time datetime="2020-01-02">Jan 2</time></body></html>'
    result = interpret_page(rule, body, url="https://x.com")
    assert len(result.items) == 1  # 整页一条（无 item_locator）
    item = result.items[0]
    assert item.fields["published_at"].startswith("2020-01-02")  # dateparser → ISO
    assert item.fields["missing"] == "N/A"  # 定位落空 → default 兜底（字段级降级不丢记录）


def test_extended_clean_ops() -> None:
    body = (
        b'<div id="t">  hello WORLD  </div><div id="p">$1,234.50</div>'
        b'<div id="n">Qty: 42 items</div><div id="r">a1b2c3</div>'
    )
    rule = c.RulePack(
        fields=[
            c.FieldRule(
                name="title",
                locator=c.Locator(type=c.LocatorType.CSS, expr="#t"),
                clean=[c.CleanOp(op="collapse_ws"), c.CleanOp(op="title")],
            ),
            c.FieldRule(
                name="price",
                locator=c.Locator(type=c.LocatorType.CSS, expr="#p"),
                clean=[c.CleanOp(op="float")],
            ),
            c.FieldRule(
                name="qty",
                locator=c.Locator(type=c.LocatorType.CSS, expr="#n"),
                clean=[c.CleanOp(op="int")],
            ),
            c.FieldRule(
                name="masked",
                locator=c.Locator(type=c.LocatorType.CSS, expr="#r"),
                clean=[c.CleanOp(op="regex_replace", params={"pattern": r"\d", "repl": "#"})],
            ),
        ],
    )
    item = interpret_page(rule, body, url="https://x.com").items[0]
    assert item.fields["title"] == "Hello World"
    assert item.fields["price"] == 1234.50
    assert item.fields["qty"] == 42
    assert item.fields["masked"] == "a#b#c#"
