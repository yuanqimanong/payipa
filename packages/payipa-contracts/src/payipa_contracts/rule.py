"""规则包 schema（声明式 spec + 版本状态机）。

声明式 spec 由内置解释器执行（不进沙箱）；不够用挂 Python 脚本（固定方法名，经代码执行器）。
清洗算子完整清单实现期定（SDD §5.1 / §14）；M1 先落一个子集。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from payipa_contracts._annotate import active, reserved
from payipa_contracts.enums import CrawlStrategy, FieldType, LocatorType, RuleStatus


class Locator(BaseModel):
    """字段定位器。"""

    type: LocatorType = active("定位方式 xpath/css/jsonpath/regex")
    expr: str = active("定位表达式（css 可含 @attr 取属性）")


class CleanOp(BaseModel):
    """清洗算子（声明式工具箱）。op 名与参数在 M1 定首批子集。"""

    op: str = active("算子名，如 strip/regex_extract/parse_date/url_normalize", since="M1")
    params: dict[str, Any] = active("算子参数", default_factory=dict, since="M1")


class FieldRule(BaseModel):
    """单字段规则。"""

    name: str = active("字段名（入库键）")
    locator: Locator = active("定位器")
    type: FieldType = active("存储 / 新链 / 存储+新链", default=FieldType.STORE)
    clean: list[CleanOp] = active("字段级清洗算子链", default_factory=list, since="M1")
    index: bool = active("是否提升为 STORED 生成列建索引", default=False, since="M1")


class LayoutMatch(BaseModel):
    """版型识别 = URL 正则 + 正文特殊值正则（同一 URL 形态多版型时靠正文区分）。"""

    url_regex: str = active("URL 匹配正则")
    body_regex: str | None = active("正文特殊值正则（可选）", default=None)


class FailWhen(BaseModel):
    """软失败判定（状态码 + 内容特征）。数据源级为主、版型级可覆盖。"""

    status_in: list[int] = active("命中即失败的状态码集合", default_factory=list, since="M2")
    body_contains: list[str] = active("命中即软失败的字符串", default_factory=list, since="M2")
    body_regex: list[str] = active("命中即软失败的正则", default_factory=list, since="M2")


class CrawlRules(BaseModel):
    """爬行/翻页规则。"""

    strategy: CrawlStrategy = active("遍历策略，默认广度", default=CrawlStrategy.BFS, since="M2")
    max_depth: int = active("最大深度", default=1, ge=0, since="M2")


class RulePack(BaseModel):
    """一个版型的完整声明式规则。"""

    fields: list[FieldRule] = active("字段规则列表")
    layout_match: LayoutMatch | None = active("版型识别", default=None)
    crawl: CrawlRules | None = active("爬行规则", default=None, since="M2")
    fail_when: FailWhen | None = active("软失败判定", default=None, since="M2")
    fingerprint: list[str] = active("数据指纹字段组合（排序 md5 + 唯一索引）", default_factory=list)
    script_ref: str | None = reserved(
        "挂载 Python 脚本的内容寻址引用（固定方法名，签名后执行）", default=None, since="M1"
    )


class RulePointer(BaseModel):
    """任务携带的规则指针（几十字节）：agent 内容寻址缓存 + 未命中拉取。"""

    rule_id: str = active("规则 id")
    version: int = active("规则版本号")
    content_hash: str = active("内容寻址 hash（不可变，缓存键）")


class RuleManifest(BaseModel):
    """规则包 + 版本元数据（对应 pyp.rules 表形状；传输模型）。"""

    rule_id: str = active("规则 id")
    version: int = active("版本号")
    content_hash: str = active("内容 hash")
    status: RuleStatus = active("版本状态 draft/testing/active", default=RuleStatus.DRAFT)
    source_id: str = active("所属数据源 id")
    pack: RulePack = active("规则内容")
    created_by: str | None = active("创建者", default=None)
