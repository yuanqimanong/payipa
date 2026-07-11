"""启动前安全校验单测（纯配置，无需 PG/Docker，CI 也跑）。"""

from __future__ import annotations

import pytest
from payipa.db.settings import Settings as DbSettings
from pyp_server.preflight import run_preflight
from pyp_server.settings import ServerSettings


def _server(**kw) -> ServerSettings:
    base = {"environment": "production", "rbac_enabled": True,
            "session_secret": "x" * 40, "agent_join_token": "real-join-token"}  # fmt: skip
    return ServerSettings(**{**base, **kw})


def _db(**kw) -> DbSettings:
    base = {"upload_secret": "real-upload-secret", "cred_kek": "real-kek"}
    return DbSettings(**{**base, **kw})


def test_dev_mode_never_raises() -> None:
    # dev 默认（RBAC 关 + dev 密钥）只告警不抛
    run_preflight(ServerSettings(environment="dev", rbac_enabled=False), _db())


def test_production_all_good_passes() -> None:
    run_preflight(_server(), _db())


def test_production_rejects_default_session_secret() -> None:
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        run_preflight(_server(session_secret="dev-session-secret-change-me-in-production-please"), _db())


def test_production_rejects_short_session_secret() -> None:
    with pytest.raises(RuntimeError, match="字节"):
        run_preflight(_server(session_secret="tooshort"), _db())


def test_production_requires_rbac() -> None:
    with pytest.raises(RuntimeError, match="RBAC_ENABLED"):
        run_preflight(_server(rbac_enabled=False), _db())


def test_production_rejects_default_join_token() -> None:
    with pytest.raises(RuntimeError, match="JOIN_TOKEN"):
        run_preflight(_server(agent_join_token="dev"), _db())


def test_production_rejects_default_kek_and_upload() -> None:
    with pytest.raises(RuntimeError, match="UPLOAD_SECRET"):
        run_preflight(_server(), _db(upload_secret="dev-insecure-change-me"))
    with pytest.raises(RuntimeError, match="CRED_KEK"):
        run_preflight(_server(), _db(cred_kek="dev-insecure-kek-change-me"))


def test_rejects_unimplemented_s3_in_any_mode() -> None:
    # 配置诚实（P0-16）：S3 未实现，配了任一项就开机即失败（dev/production 都查），绝不静默回退 local
    with pytest.raises(RuntimeError, match="S3"):
        run_preflight(_server(), _db(s3_endpoint="http://minio:9000"))
    with pytest.raises(RuntimeError, match="S3"):
        run_preflight(ServerSettings(environment="dev", rbac_enabled=False), _db(s3_bucket="raw"))


def test_preflight_rejects_multi_worker(monkeypatch) -> None:
    """P0-09：环境变量声明多 worker → 开机即拒。"""
    import pytest
    from pyp_server.preflight import run_preflight
    from pyp_server.settings import ServerSettings

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError, match="单 worker"):
        run_preflight(ServerSettings())
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    run_preflight(ServerSettings())  # workers=1 放行
