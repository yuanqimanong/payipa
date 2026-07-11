"""目标响应判定策略。

这里只做可解释的状态分类：接受、延迟重试、暂停等待人工复核或终止失败。它不会尝试
破解验证码、伪造身份、切换出口或绕过目标端访问控制。判定结果通过稳定原因码回传，
让主控可以统一执行 Retry-After、熔断和审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from payipa_contracts import ErrorCode

Outcome = Literal["accept", "retry", "pause", "fail"]

_MAX_RETRY_AFTER_S = 3600.0
_INSPECT_BYTES = 256 * 1024

# 强特征用于识别需要人工完成的交互式访问挑战。识别后只暂停，不自动处理挑战。
_CHALLENGE_MARKERS = (
    b"<title>just a moment",
    b"<title>attention required",
    b"verify you are human",
    b"complete the security check",
    b"cf-chl-",
    b"challenge-platform",
    b"g-recaptcha",
    b"hcaptcha",
    b"cf-turnstile",
)


@dataclass(frozen=True, slots=True)
class ResponseDecision:
    outcome: Outcome
    error_code: ErrorCode | None = None
    reason_code: str | None = None
    message: str | None = None
    retry_after_s: float | None = None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """解析 RFC 9110 Retry-After（秒数或 HTTP-date），并限制异常的超长等待值。"""
    if not value:
        return None
    value = value.strip()
    try:
        delay = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            delay = (when - (now or datetime.now(UTC))).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return min(_MAX_RETRY_AFTER_S, max(0.0, delay))


def detect_interactive_challenge(body: bytes, content_type: str | None) -> bool:
    """保守识别交互式挑战页；只检查有限前缀，避免大响应造成额外内存压力。"""
    if content_type and "html" not in content_type.lower():
        return False
    sample = body[:_INSPECT_BYTES].lower()
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


def assess_response(
    status: int,
    headers: dict[str, str],
    body: bytes,
    content_type: str | None,
    *,
    now: datetime | None = None,
) -> ResponseDecision:
    """把一次 HTTP 响应归一为主控可执行的韧性决策。"""
    retry_after_value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    retry_after = parse_retry_after(retry_after_value, now=now)
    if status in {401, 403, 451}:
        reasons = {401: "authentication_required", 403: "access_denied", 451: "legal_restriction"}
        return ResponseDecision(
            "pause",
            ErrorCode.ACCESS_PAUSED,
            reasons[status],
            f"target returned HTTP {status}; manual access review required",
        )
    if detect_interactive_challenge(body, content_type):
        return ResponseDecision(
            "pause",
            ErrorCode.ACCESS_PAUSED,
            "interactive_challenge",
            "interactive access challenge detected; manual handoff required",
        )
    if status == 429:
        return ResponseDecision(
            "retry",
            ErrorCode.THROTTLED,
            "rate_limited",
            "target requested a slower rate",
            retry_after if retry_after is not None else 30.0,
        )
    if status in {408, 425}:
        return ResponseDecision(
            "retry",
            ErrorCode.TIMEOUT,
            "request_not_ready",
            f"target returned HTTP {status}",
            retry_after if retry_after is not None else 5.0,
        )
    if status in {500, 502, 503, 504}:
        return ResponseDecision(
            "retry",
            ErrorCode.UPSTREAM,
            "upstream_unavailable",
            f"target returned HTTP {status}",
            retry_after if retry_after is not None else 15.0,
        )
    if status == 0:
        return ResponseDecision("retry", ErrorCode.NETWORK, "no_response", "target returned no HTTP response", 5.0)
    if 400 <= status < 500:
        return ResponseDecision("fail", ErrorCode.SOFT_FAIL, "http_client_error", f"target returned HTTP {status}")
    return ResponseDecision("accept")
