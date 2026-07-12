"""内置声明式规则解释器（agent 本地跑，不进沙箱——04A 定）。

输入 RulePack + 抓取响应 → 产出 Item + FieldMeta（每字段证据链）+ 新链接（follow，M2 用）。
定位：css（支持 ``sel@attr`` / 默认取文本）/ xpath / regex / jsonpath。
清洗算子（M1）：strip/lower/upper/title/collapse_ws/replace/regex_replace/regex_extract/
parse_date/url_normalize/prefix/suffix/truncate/int/float/default。
声明式不够用时挂 Python 脚本（固定方法名，经代码执行器）——M1 不涉及。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import dateparser
from jsonpath_ng.ext import parse as jsonpath_parse
from parsel import Selector
from payipa_contracts import FieldMeta, FieldType, Item, Locator, LocatorType, RulePack


@dataclass(slots=True)
class ParseResult:
    items: list[Item] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # type=link/store+link 产出的新链接（M2 follow）


def fail_when_reason(rule: RulePack, status: int, body: bytes) -> str | None:
    """Evaluate the source-specific soft-failure policy after transport acceptance."""
    policy = rule.fail_when
    if policy is None:
        return None
    if status in policy.status_in:
        return f"response status {status} matched fail_when"
    text = body.decode("utf-8", errors="replace")
    for marker in policy.body_contains:
        if marker in text:
            return "response body matched fail_when marker"
    for pattern in policy.body_regex:
        if re.search(pattern, text):
            return "response body matched fail_when regex"
    return None


def layout_mismatch_reason(rule: RulePack, body: bytes, url: str) -> str | None:
    """Return a stable reason when the fetched page does not match its declared layout."""
    layout = rule.layout_match
    if layout is None:
        return None
    if re.search(layout.url_regex, url) is None:
        return "final URL did not match layout_match.url_regex"
    if layout.body_regex is not None:
        text = body.decode("utf-8", errors="replace")
        if re.search(layout.body_regex, text) is None:
            return "response body did not match layout_match.body_regex"
    return None


def _looks_json(body: bytes) -> bool:
    head = body.lstrip()[:1]
    return head in (b"{", b"[")


def _css_first(node: Selector, expr: str) -> str | None:
    """css 定位：``a.next@href`` 取属性；否则取首个匹配节点的规范化文本。"""
    if "@" in expr:
        css, attr = expr.rsplit("@", 1)
        return node.css(f"{css.strip()}::attr({attr.strip()})").get()
    matched = node.css(expr)
    if not matched:
        return None
    return matched.xpath("normalize-space(string(.))").get()


def _xpath_first(node: Selector, expr: str) -> str | None:
    got = node.xpath(expr).get()
    return got.strip() if isinstance(got, str) else got


def _resolve(node: Selector, locator: Locator) -> str | None:
    if locator.type == LocatorType.CSS:
        return _css_first(node, locator.expr)
    if locator.type == LocatorType.XPATH:
        return _xpath_first(node, locator.expr)
    if locator.type == LocatorType.REGEX:
        match = re.search(locator.expr, node.get() or "")
        if not match:
            return None
        return match.group(match.lastindex or 0)
    return None  # jsonpath 走 JSON 分支


def _apply_clean(value: Any, ops, *, base_url: str) -> tuple[Any, list[str]]:
    warnings: list[str] = []
    result = value
    for op in ops:
        params = op.params
        try:
            if result is None and op.op not in ("default",):
                continue
            if op.op == "strip":
                result = result.strip() if isinstance(result, str) else result
            elif op.op == "lower":
                result = result.lower()
            elif op.op == "upper":
                result = result.upper()
            elif op.op == "replace":
                result = result.replace(params["old"], params.get("new", ""))
            elif op.op == "regex_extract":
                m = re.search(params["pattern"], result or "")
                result = m.group(params.get("group", 1) if m and m.lastindex else 0) if m else None
            elif op.op == "url_normalize":
                result = urljoin(base_url, result) if result else result
            elif op.op == "parse_date":
                parsed = dateparser.parse(result, settings={"TIMEZONE": params.get("default_tz", "Asia/Shanghai")})
                result = parsed.isoformat() if parsed else None
                if parsed is None:
                    warnings.append("parse_date 解析失败（保留原文）")
            elif op.op == "default":
                if result in (None, ""):
                    result = params.get("value")
            elif op.op == "title":
                result = result.title()
            elif op.op == "collapse_ws":
                result = re.sub(r"\s+", " ", result).strip()
            elif op.op == "regex_replace":
                result = re.sub(params["pattern"], params.get("repl", ""), result)
            elif op.op == "prefix":
                result = f"{params.get('value', '')}{result}"
            elif op.op == "suffix":
                result = f"{result}{params.get('value', '')}"
            elif op.op == "truncate":
                result = result[: int(params.get("length", 0))]
            elif op.op == "int":
                digits = re.sub(r"[^\d\-]", "", str(result))
                result = int(digits) if digits not in ("", "-") else None
            elif op.op == "float":
                num = re.sub(r"[^\d.\-]", "", str(result))
                result = float(num) if re.search(r"\d", num) else None
            else:
                warnings.append(f"未知算子 {op.op}")
        except Exception as exc:  # noqa: BLE001  单算子失败不丢整条记录（字段级降级）
            warnings.append(f"算子 {op.op} 失败: {exc}")
    return result, warnings


def _contexts(sel: Selector, rule: RulePack) -> list[Selector]:
    """列表页：按 item_locator 选行；否则整页一条。"""
    loc = rule.item_locator
    if loc is None:
        return [sel]
    if loc.type == LocatorType.CSS:
        return list(sel.css(loc.expr))
    if loc.type == LocatorType.XPATH:
        return list(sel.xpath(loc.expr))
    return [sel]


def _build_item(node: Selector, rule: RulePack, base_url: str) -> tuple[Item, list[str]]:
    fields: dict[str, Any] = {}
    field_meta: dict[str, FieldMeta] = {}
    links: list[str] = []
    for fr in rule.fields:
        raw = _resolve(node, fr.locator)
        value, warns = _apply_clean(raw, fr.clean, base_url=base_url)
        field_meta[fr.name] = FieldMeta(
            raw_value=raw if isinstance(raw, str) else (None if raw is None else str(raw)),
            normalized_value=value,
            confidence=1.0 if value not in (None, "") else 0.0,
            locator=fr.locator.expr,
            warnings=warns,
        )
        if fr.type in (FieldType.STORE, FieldType.STORE_LINK):
            fields[fr.name] = value
        if fr.type in (FieldType.LINK, FieldType.STORE_LINK) and value:
            links.append(value)
    return Item(fields=fields, field_meta=field_meta), links


def _interpret_json(rule: RulePack, body: bytes, base_url: str) -> ParseResult:
    data = json.loads(body)
    if rule.item_locator and rule.item_locator.type == LocatorType.JSONPATH:
        rows = [m.value for m in jsonpath_parse(rule.item_locator.expr).find(data)]
    else:
        rows = [data]
    result = ParseResult()
    for row in rows:
        fields: dict[str, Any] = {}
        field_meta: dict[str, FieldMeta] = {}
        for fr in rule.fields:
            matches = jsonpath_parse(fr.locator.expr).find(row) if fr.locator.type == LocatorType.JSONPATH else []
            raw = matches[0].value if matches else None
            value, warns = _apply_clean(raw, fr.clean, base_url=base_url)
            field_meta[fr.name] = FieldMeta(
                raw_value=raw if isinstance(raw, str) else (None if raw is None else str(raw)),
                normalized_value=value,
                confidence=1.0 if value not in (None, "") else 0.0,
                locator=fr.locator.expr,
                warnings=warns,
            )
            if fr.type in (FieldType.STORE, FieldType.STORE_LINK):
                fields[fr.name] = value
            if fr.type in (FieldType.LINK, FieldType.STORE_LINK) and value:
                result.links.append(value)
        result.items.append(Item(fields=fields, field_meta=field_meta))
    return result


def interpret_page(rule: RulePack, body: bytes, url: str, content_type: str | None = None) -> ParseResult:
    """解析一页 → ParseResult(items+links)。字段级降级：单字段失败不丢整条记录。"""
    is_json = (content_type is not None and "json" in content_type.lower()) or _looks_json(body)
    if is_json:
        return _interpret_json(rule, body, url)

    sel = Selector(body.decode("utf-8", errors="replace"))
    result = ParseResult()
    for node in _contexts(sel, rule):
        item, links = _build_item(node, rule, base_url=url)
        result.items.append(item)
        result.links.extend(links)
    return result
