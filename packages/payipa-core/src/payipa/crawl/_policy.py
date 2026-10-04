"""采集策略纯函数：授权记录、URL 指纹、域名边界与回报 fencing；无 I/O。"""

from __future__ import annotations

from urllib.parse import urlsplit

from jianbing_utils import crypto
from payipa_contracts import (
    RequestState,
)

ACCESS_BASES = frozenset({"owned", "contracted", "public_policy"})


def _validate_access_record(access_basis: str | None, access_reference: str | None) -> tuple[str, str]:
    basis = (access_basis or "").strip()
    reference = (access_reference or "").strip()
    if basis not in ACCESS_BASES:
        raise ValueError(f"access_basis 必须是 {', '.join(sorted(ACCESS_BASES))} 之一")
    if not reference:
        raise ValueError("access_reference 必须记录授权文件、合同、API 文档或公开访问政策")
    return basis, reference


def url_fingerprint(url: str) -> str:
    """URL 去重指纹：最小规范化（去 fragment + 去首尾空白）后 sha256。

    完整规范化（查询参数排序/百分号归一/黑白名单）由 jianbing_utils 自研模块承接（决策：URL 规范化自研），
    此处先用最小实现保证批内同 URL 去重正确。
    """
    normalized = url.split("#", 1)[0].strip()
    return crypto.sha256(normalized)


def _allowed_domains(params: dict, target: str) -> list[str]:
    """任务出网边界：显式白名单优先，否则固定为该数据源已存档种子域名。"""
    explicit = params.get("allowed_domains")
    candidates = explicit if isinstance(explicit, list) and explicit else params.get("seed_urls") or [target]
    domains: list[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        host = urlsplit(value if "://" in value else f"//{value}").hostname
        host = (host or "").strip().rstrip(".").lower()
        if host and host not in domains:
            domains.append(host)
    return domains


def _is_stale(row, agent_id: str | None, attempt: int | None) -> bool:
    """迟到/越权回报判定（P0-10 fencing）：终态、归属不符或代次不符的回报一律视为 stale。"""
    if row is None:
        return True
    if int(row.state) not in (int(RequestState.QUEUED), int(RequestState.ASSIGNED), int(RequestState.RUNNING)):
        return True  # 已终结（成功/取消/失败）：迟到结果不得覆盖
    if agent_id is not None and row.agent_id != agent_id:
        return True  # 回报者不是当前持有者（重派/回收后 agent_id 已换或清空）
    return attempt is not None and int(row.attempt or 0) != attempt


_INFLIGHT = (int(RequestState.ASSIGNED), int(RequestState.RUNNING))  # 「在途」= 已占用未终结
