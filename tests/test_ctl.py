"""pypctl 纯单元测试（P0-03；无 Docker/网络/PG）。

覆盖：init 的本地/生产配置、配置前置诊断、隐藏 Agent 入网码输入；
status/smoke 对 monkeypatch 的 probe 探测函数正确汇总（就绪/未就绪/revision 不一致）。
"""

from __future__ import annotations

from pyp_server import ctl
from pyp_server.settings import DEV_SESSION_SECRET

_SECRET_KEYS = ("PG_PASSWORD", "PYP_SERVER_SESSION_SECRET", "PYP_SERVER_BOOTSTRAP_TOKEN", "UPLOAD_SECRET", "CRED_KEK")
# 各 dev 默认值（settings/preflight 同源）：生成的密钥绝不允许撞上
_DEV_VALUES = {
    "dev",
    "postgres",
    "dev-insecure-upload-secret-change-me",
    "dev-insecure-kek-change-me",
    DEV_SESSION_SECRET,
}


def _parse_env(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))


def _make_repo(tmp_path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "compose.yml").write_text("services: {}\n", encoding="utf-8")


def test_init_secrets_unique_and_non_default(tmp_path, monkeypatch) -> None:
    _make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert ctl.main(["init"]) == 0
    env = _parse_env((tmp_path / "deploy" / ".env.compose").read_text(encoding="utf-8"))
    vals = [env[k] for k in _SECRET_KEYS]
    assert len(set(vals)) == len(vals), "各密钥必须互不相同（域分离）"
    for v in vals:
        assert v not in _DEV_VALUES
        assert len(v) >= 24
    assert len(env["PYP_SERVER_SESSION_SECRET"].encode()) >= 32  # preflight 的 HS256 下限
    assert env["PG_HOST"] == "db"  # compose 内网服务名，不指宿主
    assert env["DATA_ROOT"] == "/data"
    assert env["PYP_DEPLOY_BIND_ADDRESS"] == "127.0.0.1"
    assert env["PYP_SERVER_ENVIRONMENT"] == "dev"
    assert env["PYP_SERVER_ALLOWED_HOSTS"] == ctl.LOCAL_ALLOWED_HOSTS
    assert env["PYP_SERVER_RBAC_ENABLED"] == "false"


def test_init_refuses_overwrite(tmp_path, monkeypatch) -> None:
    _make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert ctl.main(["init"]) == 0
    before = (tmp_path / "deploy" / ".env.compose").read_text(encoding="utf-8")
    assert ctl.main(["init"]) == 1  # 二次 init 拒绝覆盖
    assert (tmp_path / "deploy" / ".env.compose").read_text(encoding="utf-8") == before


def test_init_production_creates_strict_baseline(tmp_path, monkeypatch) -> None:
    _make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert ctl.main(["init", "--production-host", "PYP.Example.COM"]) == 0
    env = _parse_env((tmp_path / "deploy" / ".env.compose").read_text(encoding="utf-8"))
    assert env["PYP_SERVER_ENVIRONMENT"] == "production"
    assert env["PYP_SERVER_RBAC_ENABLED"] == "true"
    assert env["PYP_SERVER_ALLOWED_HOSTS"] == f"{ctl.LOCAL_ALLOWED_HOSTS},pyp.example.com"
    assert ctl._deployment_config_problems(env) == []


def test_init_refuses_outside_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert ctl.main(["init"]) == 1
    assert not (tmp_path / "deploy").exists()


def test_deployment_config_rejects_production_rbac_and_duplicate_databases() -> None:
    env = _parse_env(ctl.gen_env("pyp.example.com"))
    env["PYP_SERVER_RBAC_ENABLED"] = "false"
    env["PG_DB_BUSINESS"] = env["PG_DB_DATA_CENTER"]
    env["PYP_SERVER_ALLOWED_HOSTS"] = "*.example.com"
    problems = ctl._deployment_config_problems(env)
    assert any("RBAC" in problem for problem in problems)
    assert any("互不相同" in problem for problem in problems)
    assert any("ALLOWED_HOSTS" in problem for problem in problems)


def test_agent_prompts_for_token_without_exposing_it(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(ctl.getpass, "getpass", lambda _prompt: "one-time-token")

    def fake_start_agent(token: str, build: bool = False) -> int:
        captured["token"] = token
        captured["build"] = build
        return 0

    monkeypatch.setattr(ctl, "start_agent", fake_start_agent)
    assert ctl.main(["agent", "--build"]) == 0
    assert captured == {"token": "one-time-token", "build": True}


def _fake_probe(responses: dict[str, tuple[int, dict]]):
    def fake(path: str, base: str = ctl.BASE_URL, timeout: float = 5.0) -> tuple[int, dict]:
        return responses[path]

    return fake


_SCHEMA_OK = {"expected_head": "abc123", "pyp": "abc123", "data_center": "abc123", "business": "abc123"}


def test_status_green(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ctl,
        "probe",
        _fake_probe(
            {
                "/livez": (200, {"status": "ok"}),
                "/readyz": (200, {"status": "ready", "checks": {"db.pyp": "ok"}}),
                "/version": (200, {"server": "0.1.0", "contracts": 1, "commit": "deadbee", "schema": _SCHEMA_OK}),
            }
        ),
    )
    assert ctl.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "livez : 200" in out
    assert "abc123" in out


def test_status_down_returns_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        ctl,
        "probe",
        _fake_probe(
            {
                "/livez": (0, {"error": "ConnectionRefusedError"}),
                "/readyz": (0, {"error": "ConnectionRefusedError"}),
                "/version": (0, {"error": "ConnectionRefusedError"}),
            }
        ),
    )
    assert ctl.main(["status"]) == 1


def test_smoke_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ctl,
        "probe",
        _fake_probe(
            {
                "/readyz": (200, {"status": "ready", "checks": {}}),
                "/version": (200, {"schema": _SCHEMA_OK}),
            }
        ),
    )
    assert ctl.main(["smoke"]) == 0
    out = capsys.readouterr().out
    assert "冒烟通过" in out
    assert "/setup" in out  # 下一步提示：浏览器首启引导


def test_smoke_fails_when_not_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ctl,
        "probe",
        _fake_probe({"/readyz": (503, {"status": "unavailable", "checks": {"migrations": "error: 未初始化"}})}),
    )
    assert ctl.main(["smoke"]) == 1
    assert "migrations" in capsys.readouterr().out


def test_smoke_fails_on_revision_mismatch(monkeypatch, capsys) -> None:
    schema = dict(_SCHEMA_OK, business="fff999")  # 有一库没迁到 head
    monkeypatch.setattr(
        ctl,
        "probe",
        _fake_probe(
            {
                "/readyz": (200, {"status": "ready", "checks": {}}),
                "/version": (200, {"schema": schema}),
            }
        ),
    )
    assert ctl.main(["smoke"]) == 1
    assert "business" in capsys.readouterr().out
