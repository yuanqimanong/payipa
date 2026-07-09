"""job_token JWT 单测（无需 PG）：签发/校验 + 受众/过期/篡改 + scope。"""

from __future__ import annotations

import jwt
import pytest
from payipa.security.job_token import issue_job_token, token_allows_table, verify_job_token

_SECRET = "test-job-secret-please-change"


def test_issue_verify_roundtrip() -> None:
    tok, jti = issue_job_token(_SECRET, "job-1", tables=["data_src", "asm_prod"], row_quota=1000, lease_s=60)
    assert tok and jti
    claims = verify_job_token(_SECRET, tok, "job-1")
    assert claims is not None
    assert claims["aud"] == "job-1" and claims["jti"] == jti
    assert claims["scope"]["tables"] == ["data_src", "asm_prod"]
    assert claims["scope"]["row_quota"] == 1000
    assert token_allows_table(claims, "data_src") and not token_allows_table(claims, "pyp_users")


def test_wrong_audience_rejected() -> None:
    tok, _ = issue_job_token(_SECRET, "job-1", tables=["data_src"])
    assert verify_job_token(_SECRET, tok, "job-2") is None  # 受众不符 → 拒


def test_tampered_or_bad_secret_rejected() -> None:
    tok, _ = issue_job_token(_SECRET, "job-1", tables=["data_src"])
    assert verify_job_token("another-secret", tok, "job-1") is None  # 签名不符
    assert verify_job_token(_SECRET, tok + "x", "job-1") is None  # 篡改


def test_expired_rejected() -> None:
    tok, _ = issue_job_token(_SECRET, "job-1", tables=["data_src"], lease_s=-1)  # 已过期
    assert verify_job_token(_SECRET, tok, "job-1") is None


def test_decode_requires_audience() -> None:
    # 不带 audience 解码会因 aud 存在而报错（证明 aud 是强校验项）
    tok, _ = issue_job_token(_SECRET, "job-1", tables=["data_src"])
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(tok, _SECRET, algorithms=["HS256"])  # 缺 audience 参数 → InvalidAudience/Missing
