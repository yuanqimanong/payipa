"""Agent 响应策略：Retry-After、交互挑战识别与状态分类。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from payipa_contracts import ErrorCode
from pyp_agent.response_policy import assess_response, parse_retry_after


def test_retry_after_seconds_and_http_date_are_bounded() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    assert parse_retry_after("12", now=now) == 12
    assert parse_retry_after(format_datetime(now + timedelta(seconds=45)), now=now) == 45
    assert parse_retry_after("999999", now=now) == 3600
    assert parse_retry_after("not-a-date", now=now) is None


def test_rate_limit_and_upstream_are_retryable() -> None:
    limited = assess_response(429, {"Retry-After": "17"}, b"", "text/plain")
    assert limited.outcome == "retry"
    assert limited.error_code == ErrorCode.THROTTLED
    assert limited.retry_after_s == 17

    unavailable = assess_response(503, {}, b"", "text/html")
    assert unavailable.outcome == "retry"
    assert unavailable.error_code == ErrorCode.UPSTREAM
    assert unavailable.retry_after_s == 15

    immediate = assess_response(429, {"retry-after": "0"}, b"", "text/plain")
    assert immediate.retry_after_s == 0


def test_interactive_challenge_pauses_without_processing_it() -> None:
    decision = assess_response(
        200,
        {},
        b"<html><title>Just a moment...</title><div>Verify you are human</div></html>",
        "text/html; charset=utf-8",
    )
    assert decision.outcome == "pause"
    assert decision.error_code == ErrorCode.ACCESS_PAUSED
    assert decision.reason_code == "interactive_challenge"


def test_normal_json_response_is_accepted() -> None:
    assert assess_response(200, {}, b'{"ok":true}', "application/json").outcome == "accept"
