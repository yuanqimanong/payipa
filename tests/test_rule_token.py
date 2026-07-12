"""规则读取 token：绑定内容 hash、过期校验，并与上传 token 做签名域隔离。"""

from payipa.security.tokens import (
    issue_rule_token,
    issue_upload_token,
    verify_rule_token,
)


def test_rule_token_is_hash_bound_and_domain_separated() -> None:
    secret = "test-secret-with-enough-entropy"
    token = issue_rule_token(secret, "hash-a", ttl_s=60)
    assert verify_rule_token(secret, token, "hash-a") is True
    assert verify_rule_token(secret, token, "hash-b") is False

    upload = issue_upload_token(secret, "hash-a", "rules", ttl_s=60)
    assert verify_rule_token(secret, upload, "hash-a") is False


def test_expired_rule_token_is_rejected(monkeypatch) -> None:
    import payipa.security.tokens as tokens

    monkeypatch.setattr(tokens.time, "time", lambda: 1000)
    token = issue_rule_token("secret", "hash-a", ttl_s=1)
    monkeypatch.setattr(tokens.time, "time", lambda: 1002)
    assert verify_rule_token("secret", token, "hash-a") is False
