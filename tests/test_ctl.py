"""pypctl 纯单元测试（P0-03；无 Docker/网络/PG）。

覆盖：init 的本地/生产配置、配置前置诊断、隐藏 Agent 入网码输入；
status/smoke 对 monkeypatch 的 probe 探测函数正确汇总（就绪/未就绪/revision 不一致）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
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


@pytest.mark.parametrize("missing", ["pyp", "data_center", "business"])
def test_smoke_requires_every_database_revision(missing, monkeypatch) -> None:
    schema = {key: value for key, value in _SCHEMA_OK.items() if key != missing}
    monkeypatch.setattr(ctl, "probe", _fake_probe({"/readyz": (200, {}), "/version": (200, {"schema": schema})}))
    assert ctl.smoke(ctl.BASE_URL) == 1


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    _make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    ctl.ENV_FILE.write_text(ctl.gen_env(), encoding="utf-8")
    monkeypatch.setattr(ctl, "probe", lambda *_args, **_kwargs: (200, {"schema": _SCHEMA_OK}))
    return tmp_path


@pytest.fixture
def saved_backup(deployment):
    root = deployment / "saved-backup"
    root.mkdir()
    database_names = ["pyp_sys", "data_center", "business"]
    for name in [*(f"{db}.dump" for db in database_names), "pyp_data.tar.gz"]:
        (root / name).write_bytes(b"synthetic backup data")
    (root / "env.compose").write_bytes(ctl.ENV_FILE.read_bytes())
    manifest = {
        "created_at": "2026-10-11T00:00:00+00:00",
        "databases": database_names,
        "version": {"schema": _SCHEMA_OK},
        "files": {path.name: {"size": path.stat().st_size, "sha256": ctl._sha256(path)} for path in root.iterdir()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_upgrade_stops_after_failed_stop(deployment, monkeypatch) -> None:
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(ctl.subprocess, "run", run)
    monkeypatch.setattr(ctl, "backup", lambda **kwargs: pytest.fail("不得在停止失败后备份"))
    assert ctl.upgrade() == 17
    assert len(calls) == 1 and "stop" in calls[0]


def test_restore_stops_after_failed_stop(saved_backup, monkeypatch) -> None:
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(ctl.subprocess, "run", run)
    monkeypatch.setattr(ctl, "_run_binary", lambda *args, **kwargs: pytest.fail("不得在停止失败后恢复数据"))
    assert ctl.restore(str(saved_backup), "RESTORE") == 17
    assert len(calls) == 1 and "stop" in calls[0]


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "missing_database",
        "duplicate_database",
        "different_database",
        "missing_dump",
        "missing_config",
        "missing_storage",
        "bad_sha256",
        "missing_sha256",
        "bad_size",
        "bad_metadata",
        "not_a_mapping",
        "corrupt_json",
        "corrupt_dump",
        "missing_file",
        "outside_path",
    ],
)
def test_restore_rejects_incomplete_backup_before_stopping(saved_backup, monkeypatch, damage) -> None:
    manifest_path = saved_backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "empty":
        manifest = {"files": {}, "databases": []}
    elif damage == "missing_database":
        manifest["databases"].pop()
    elif damage == "duplicate_database":
        manifest["databases"][2] = "pyp_sys"
    elif damage == "different_database":
        manifest["databases"][2] = "other_business"
    elif damage.startswith("missing_") and damage in {"missing_dump", "missing_config", "missing_storage"}:
        name = {"missing_dump": "business.dump", "missing_config": "env.compose", "missing_storage": "pyp_data.tar.gz"}[
            damage
        ]
        del manifest["files"][name]
    elif damage == "bad_sha256":
        manifest["files"]["business.dump"]["sha256"] = 123
    elif damage == "missing_sha256":
        del manifest["files"]["business.dump"]["sha256"]
    elif damage == "bad_size":
        manifest["files"]["business.dump"]["size"] = -1
    elif damage == "bad_metadata":
        manifest["files"]["business.dump"] = None
    elif damage == "not_a_mapping":
        manifest = []
    elif damage == "corrupt_dump":
        (saved_backup / "business.dump").write_bytes(b"corrupted")
    elif damage == "missing_file":
        (saved_backup / "business.dump").unlink()
    elif damage == "outside_path":
        manifest["files"]["../outside.dump"] = manifest["files"]["business.dump"]
    manifest_path.write_text("{" if damage == "corrupt_json" else json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(ctl.subprocess, "run", lambda *args, **kwargs: pytest.fail("校验失败前不得停服"))
    monkeypatch.setattr(ctl, "_run_binary", lambda *args, **kwargs: pytest.fail("校验失败前不得恢复"))
    assert ctl.restore(str(saved_backup), "RESTORE") != 0


def test_restore_accepts_existing_manifest_format(saved_backup, monkeypatch) -> None:
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0)

    def binary(cmd, *args, **kwargs):
        commands.append(cmd)
        return 0, ""

    monkeypatch.setattr(ctl.subprocess, "run", run)
    monkeypatch.setattr(ctl, "_run_binary", binary)
    assert ctl.restore(str(saved_backup), "RESTORE") == 0
    assert [cmd[-1] for cmd in commands if "pg_restore" in cmd] == ["pyp_sys", "data_center", "business"]
    assert "stop" in commands[0] and "up" in commands[-1]


def test_restore_accepts_matching_custom_database_names(saved_backup, monkeypatch) -> None:
    manifest_path = saved_backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = ctl.ENV_FILE.read_text(encoding="utf-8")
    for old in manifest["databases"]:
        new = f"custom_{old}"
        config = config.replace(f"={old}\n", f"={new}\n")
        (saved_backup / f"{old}.dump").rename(saved_backup / f"{new}.dump")
        manifest["files"][f"{new}.dump"] = manifest["files"].pop(f"{old}.dump")
    manifest["databases"] = [f"custom_{db}" for db in manifest["databases"]]
    ctl.ENV_FILE.write_text(config, encoding="utf-8")
    (saved_backup / "env.compose").write_text(config, encoding="utf-8")
    manifest["files"]["env.compose"] = {
        "size": (saved_backup / "env.compose").stat().st_size,
        "sha256": ctl._sha256(saved_backup / "env.compose"),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restored = []

    def binary(cmd, *args, **kwargs):
        if "pg_restore" in cmd:
            restored.append(cmd[-1])
        return 0, ""

    monkeypatch.setattr(ctl.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(ctl, "_run_binary", binary)
    assert ctl.restore(str(saved_backup), "RESTORE") == 0
    assert restored == manifest["databases"]


@pytest.mark.parametrize("key", ["CRED_KEK", "PG_DB_PYP"])
def test_restore_requires_matching_current_configuration(saved_backup, monkeypatch, key) -> None:
    values = ctl._env_values()
    config = ctl.ENV_FILE.read_text(encoding="utf-8").replace(f"{key}={values[key]}", f"{key}=different-value")
    ctl.ENV_FILE.write_text(config, encoding="utf-8")
    monkeypatch.setattr(ctl.subprocess, "run", lambda *args, **kwargs: pytest.fail("配置不一致时不得停服"))
    assert ctl.restore(str(saved_backup), "RESTORE") == 1


def test_backup_rejects_unknown_service_state(deployment, monkeypatch) -> None:
    monkeypatch.setattr(ctl, "_run", lambda *args, **kwargs: (23, "cannot inspect services"))
    monkeypatch.setattr(ctl, "_run_binary", lambda *args, **kwargs: pytest.fail("状态未知时不得备份"))
    assert ctl.backup(str(deployment / "backup")) == (23, None)


def test_upgrade_backup_requires_server_to_remain_stopped(deployment, monkeypatch) -> None:
    monkeypatch.setattr(ctl, "_run", lambda *args, **kwargs: (0, "server\n"))
    monkeypatch.setattr(ctl, "_run_binary", lambda *args, **kwargs: pytest.fail("主控运行时不得冷备"))
    assert ctl.backup(str(deployment / "backup"), manage_server=False) == (1, None)


@pytest.mark.parametrize("dump_code,restart_code", [(0, 0), (0, 19), (11, 0), (11, 19)])
def test_backup_reports_failure_and_preserves_completed_backup(
    deployment, monkeypatch, dump_code, restart_code
) -> None:
    commands = []
    monkeypatch.setattr(ctl, "_run", lambda *args, **kwargs: (0, "db\nserver\n"))

    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=restart_code if "up" in cmd else 0)

    def binary(cmd, path, **kwargs):
        path.write_bytes(b"synthetic backup data")
        return dump_code, "simulated failure" if dump_code else ""

    monkeypatch.setattr(ctl.subprocess, "run", run)
    monkeypatch.setattr(ctl, "_run_binary", binary)
    target = deployment / "backup"
    code, result = ctl.backup(str(target))
    assert code == (dump_code or restart_code)
    assert result == (None if dump_code else target)
    assert (target / "manifest.json").is_file() is (dump_code == 0)
    assert len(commands) == 2 and "stop" in commands[0] and "up" in commands[1]


@pytest.mark.parametrize("operation", ["up", "agent", "config", "binary", "down"])
def test_compose_uses_file_config_and_keeps_runtime_environment(deployment, monkeypatch, operation) -> None:
    monkeypatch.setenv("PG_PASSWORD", "inherited-wrong-password")
    monkeypatch.setenv("PG_DB_PYP", "inherited_wrong_database")
    monkeypatch.setenv("DOCKER_CONTEXT", "selected-context")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8123")
    monkeypatch.setenv("PYP_AGENT_ENROLL_TOKEN", "stale-token")
    captured = []

    def run(cmd, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr=b"" if operation == "binary" else "")

    monkeypatch.setattr(ctl.subprocess, "run", run)
    if operation == "up":
        assert ctl.up() == 0
    elif operation == "agent":
        assert ctl.start_agent("explicit-token") == 0
    elif operation == "config":
        assert ctl._run(ctl._compose("config", "-q"))[0] == 0
    elif operation == "binary":
        assert ctl._run_binary(ctl._compose("exec", "db", "pg_dump"), deployment / "dump")[0] == 0
    else:
        assert ctl.down() == 0
    child_env = captured[0]["env"]
    assert "PG_PASSWORD" not in child_env and "PG_DB_PYP" not in child_env
    assert child_env["DOCKER_CONTEXT"] == "selected-context"
    assert child_env["HTTPS_PROXY"] == "http://proxy.example:8123"
    assert child_env["PATH"] == ctl.os.environ["PATH"]
    if operation == "agent":
        assert child_env["PYP_AGENT_ENROLL_TOKEN"] == "explicit-token"


def test_compose_defaults_ignore_inherited_database_names(deployment, monkeypatch) -> None:
    ctl.ENV_FILE.write_text("PG_PASSWORD=deployment-value\n", encoding="utf-8")
    monkeypatch.setenv("PG_DB_PYP", "inherited_wrong_database")
    monkeypatch.setenv("PG_USER", "inherited_wrong_user")
    env = ctl._compose_environment()
    assert "PG_DB_PYP" not in env and "PG_USER" not in env
