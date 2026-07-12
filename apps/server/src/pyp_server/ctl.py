"""pypctl —— 主控部署运维命令（P0-03）：init / doctor / up / down / status / smoke。

包装 deploy/compose.yml 的容器化一键路径（P0-02）；纯 stdlib，须从仓库根运行
（compose 文件与 env 文件均按仓库相对路径寻址）。冒烟（v1）只验 readyz 全绿 +
三库 revision 一致；全链路 fixture 采集冒烟随首启向导（P0-21）补齐。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

COMPOSE = Path("deploy/compose.yml")
ENV_FILE = Path("deploy/.env.compose")
BASE_URL = "http://127.0.0.1:8100"  # compose 把 server 映射到宿主 8100（8000 留给本机 dev 主控）
HOST_PORT = 8100


def gen_env() -> str:
    """生成 .env.compose 内容：各密钥独立随机（secrets.token_urlsafe），互不复用（域分离）。"""
    tok = secrets.token_urlsafe
    return (
        "# 由 pypctl init 生成（勿入库；.gitignore 已屏蔽）。compose 用法见 deploy/compose.yml 头注释。\n"
        "# ⚠️ 生产必改：PYP_SERVER_ENVIRONMENT=production + PYP_SERVER_RBAC_ENABLED=true\n"
        "#    （见 docs/install/docker-compose.md 与 deploy/.env.compose.example 的生产必改项清单）。\n"
        "PG_HOST=db\n"
        "PG_PORT=5432\n"
        "PG_USER=postgres\n"
        f"PG_PASSWORD={tok(24)}\n"
        "PG_DB_PYP=pyp_sys\n"
        "PG_DB_DATA_CENTER=data_center\n"
        "PG_DB_BUSINESS=business\n"
        f"PYP_SERVER_SESSION_SECRET={tok(48)}\n"
        f"PYP_SERVER_BOOTSTRAP_TOKEN={tok(32)}\n"
        f"UPLOAD_SECRET={tok(48)}\n"
        f"CRED_KEK={tok(48)}\n"
        "DATA_ROOT=/data\n"
        "PYP_SERVER_ENVIRONMENT=dev\n"
    )


def probe(path: str, base: str = BASE_URL, timeout: float = 5.0) -> tuple[int, dict]:
    """GET base+path，返回 (HTTP 状态, JSON body)。连不上返回 (0, {"error": …})。

    status/smoke 共用；单测 monkeypatch 本函数即可离线验证汇总逻辑。
    """
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # readyz 503 也带分项 JSON，照样解析
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001 —— 连接拒绝/超时等
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑一条外部命令，返回 (退出码, 合并输出)。找不到命令/超时不抛，转成非 0 码。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（>{timeout}s）：{' '.join(cmd)}"


def _compose(*args: str) -> list[str]:
    cmd = ["docker", "compose", "-f", str(COMPOSE)]
    if ENV_FILE.exists():
        cmd += ["--env-file", str(ENV_FILE)]
    return cmd + list(args)


def _port_free(port: int) -> bool:
    """宿主端口是否空闲（能 connect 上说明已被占用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_binary(cmd: list[str], path: Path, *, stdin_path: Path | None = None) -> tuple[int, str]:
    """执行二进制备份/恢复命令；数据只进文件/stdin，stderr 才作为诊断文本。"""
    source = stdin_path.open("rb") if stdin_path else None
    target = path.open("wb") if stdin_path is None else subprocess.DEVNULL
    try:
        proc = subprocess.run(cmd, stdin=source, stdout=target, stderr=subprocess.PIPE, check=False)
    finally:
        if source is not None:
            source.close()
        if target is not subprocess.DEVNULL:
            target.close()
    return proc.returncode, proc.stderr.decode(errors="replace").strip()


# ── 子命令 ─────────────────────────────────────────────────────────────


def init() -> int:
    """生成 deploy/.env.compose（已存在则拒绝覆盖——密钥一旦投用不可随意轮换）。"""
    if ENV_FILE.exists():
        print(f"已存在 {ENV_FILE}，拒绝覆盖。确要重生成请先手工删除该文件（会作废在用密钥与会话）。")
        return 1
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(gen_env(), encoding="utf-8", newline="\n")
    print(f"已生成 {ENV_FILE}（各密钥独立随机）。下一步：uv run pypctl up --build")
    return 0


def doctor(url: str) -> int:
    """环境体检：docker / compose 可用、compose 文件语法、宿主端口空闲、（在跑时）readyz。"""
    checks: list[tuple[str, bool, str]] = []
    code, out = _run(["docker", "--version"])
    checks.append(("docker", code == 0, out.splitlines()[0] if out else ""))
    code, out = _run(["docker", "compose", "version"])
    checks.append(("docker compose", code == 0, out.splitlines()[0] if out else ""))
    if not ENV_FILE.exists():
        checks.append(("env 文件", False, f"缺 {ENV_FILE}：先 uv run pypctl init"))
    else:
        code, out = _run(_compose("config", "-q"))
        checks.append(("compose 语法", code == 0, out or str(COMPOSE)))
    if _port_free(HOST_PORT):
        checks.append((f"端口 {HOST_PORT}", True, "空闲"))
    else:  # 被占：若正是已启动的 payipa server，readyz 探测给出真实状态
        rcode, ready = probe("/readyz", url)
        occupier = f"readyz={rcode} {ready.get('status', ready.get('error', ''))}"
        checks.append((f"端口 {HOST_PORT}", rcode == 200, f"被占用（{occupier}；若是已启动的 payipa 属正常）"))
    ok = all(good for _, good, _ in checks)
    for name, good, detail in checks:  # 标记用 ASCII：Windows 控制台 GBK 编码印不出 ✓/✗
        print(f"  [{'OK' if good else '!!'}] {name}: {detail}")
    print("体检通过" if ok else "体检未通过：按上方 [!!] 项修复后重试")
    return 0 if ok else 1


def up(build: bool = False, agents: bool = False) -> int:
    """按依赖顺序启动：db → migrate（one-shot）→ server（readyz 健康门）→ 可选 agent。"""
    if not ENV_FILE.exists():
        print(f"缺 {ENV_FILE}：先 uv run pypctl init")
        return 1
    if agents:
        print("首次部署不能同时接入 Agent：先执行 pypctl up，创建管理员后在节点页生成一次性入网码，")
        print("再执行：pypctl agent --token <一次性入网码>")
        return 2
    cmd = _compose("up", "-d", "--wait", *(["--build"] if build else []))
    print("$", " ".join(cmd))
    code = subprocess.run(cmd, check=False).returncode  # 输出直通终端，便于看拉镜像/构建进度
    if code == 0:
        print(f"已就绪。看状态：uv run pypctl status；冒烟：uv run pypctl smoke；页面：{BASE_URL}/setup")
    return code


def start_agent(token: str, build: bool = False) -> int:
    """用 UI 签发的一次性入网码启动 compose 中的单 Agent，并持久化独立身份。"""
    if not ENV_FILE.exists():
        print(f"缺 {ENV_FILE}：先 uv run pypctl init")
        return 1
    env = os.environ.copy()
    env["PYP_AGENT_ENROLL_TOKEN"] = token
    cmd = _compose("--profile", "agents", "up", "-d", *(["--build"] if build else []), "agent")
    print("$", " ".join(cmd), "(PYP_AGENT_ENROLL_TOKEN=<redacted>)")
    return subprocess.run(cmd, check=False, env=env).returncode


def backup(output: str | None = None, *, manage_server: bool = True) -> tuple[int, Path | None]:
    """冷备三库 + 本地对象卷 + 部署配置，并生成带 SHA-256 的 manifest。"""
    if not ENV_FILE.exists():
        print(f"缺 {ENV_FILE}：先 uv run pypctl init")
        return 1, None
    target = Path(output) if output else Path("backups") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        print(f"备份目录非空，拒绝覆盖：{target}")
        return 1, None
    target.mkdir(parents=True, exist_ok=True)

    _version_status, version = probe("/version")
    code, running = _run(_compose("ps", "--status", "running", "--services"))
    server_was_running = code == 0 and "server" in running.splitlines()
    if manage_server and server_was_running:
        code = subprocess.run(_compose("stop", "server"), check=False).returncode
        if code:
            return code, None

    env = _env_values()
    user = env.get("PG_USER", "postgres")
    dbs = [
        env.get("PG_DB_PYP", "pyp_sys"),
        env.get("PG_DB_DATA_CENTER", "data_center"),
        env.get("PG_DB_BUSINESS", "business"),
    ]
    files: list[Path] = []
    try:
        for db in dbs:
            path = target / f"{db}.dump"
            code, error = _run_binary(_compose("exec", "-T", "db", "pg_dump", "-U", user, "--format=custom", db), path)
            if code:
                print(f"数据库 {db} 备份失败：{error}")
                return code, None
            files.append(path)

        storage = target / "pyp_data.tar.gz"
        archive_code = (
            "import sys,tarfile; "
            "t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/data',arcname='data'); t.close()"
        )
        code, error = _run_binary(
            _compose("run", "--rm", "--no-deps", "-T", "server", "python", "-c", archive_code), storage
        )
        if code:
            print(f"对象存储卷备份失败：{error}")
            return code, None
        files.append(storage)

        config_copy = target / "env.compose"
        shutil.copy2(ENV_FILE, config_copy)
        files.append(config_copy)
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "databases": dbs,
            "version": version,
            "files": {path.name: {"size": path.stat().st_size, "sha256": _sha256(path)} for path in files},
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        print(f"[OK] 冷备完成：{target}")
        print("备份包含部署密钥，请按最高敏感级别加密保管，并定期在隔离环境执行 restore 演练。")
        return 0, target
    finally:
        if manage_server and server_was_running:
            subprocess.run(_compose("up", "-d", "--wait", "server"), check=False)


def restore(backup_dir: str, confirmation: str) -> int:
    """从 pypctl 备份恢复三库与对象卷；必须显式确认，失败时保持主控停止。"""
    if confirmation != "RESTORE":
        print("恢复会覆盖当前三库和对象卷；请追加 --confirm RESTORE")
        return 2
    root = Path(backup_dir).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        print(f"缺备份清单：{manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, meta in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file() or _sha256(path) != meta.get("sha256"):
            print(f"备份校验失败：{name}")
            return 1
    current = _env_values()
    backed_up = {}
    for line in (root / "env.compose").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            backed_up[key] = value
    if current.get("CRED_KEK") != backed_up.get("CRED_KEK"):
        print("当前 CRED_KEK 与备份不一致，拒绝恢复：否则库中信封密文将不可解密。")
        return 1

    subprocess.run(_compose("stop", "server", "agent"), check=False)
    user = current.get("PG_USER", "postgres")
    for db in manifest.get("databases", []):
        path = root / f"{db}.dump"
        code, error = _run_binary(
            _compose(
                "exec",
                "-T",
                "db",
                "pg_restore",
                "-U",
                user,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                db,
            ),
            Path(os.devnull),
            stdin_path=path,
        )
        if code:
            print(f"数据库 {db} 恢复失败：{error}")
            return code

    extract_code = (
        "import pathlib,shutil,sys,tarfile; p=pathlib.Path('/data'); "
        "[(shutil.rmtree(x) if x.is_dir() else x.unlink()) for x in p.iterdir()]; "
        "tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz').extractall('/',filter='data')"
    )
    code, error = _run_binary(
        _compose("run", "--rm", "--no-deps", "-T", "server", "python", "-c", extract_code),
        Path(os.devnull),
        stdin_path=root / "pyp_data.tar.gz",
    )
    if code:
        print(f"对象卷恢复失败：{error}")
        return code
    code = subprocess.run(_compose("run", "--rm", "migrate"), check=False).returncode
    if code:
        print("恢复后的迁移失败，主控保持停止，请检查日志。")
        return code
    code = subprocess.run(_compose("up", "-d", "--wait", "server"), check=False).returncode
    if code == 0:
        print("[OK] 恢复完成；请立即执行 pypctl smoke，并重新签发 Agent 入网码。")
    return code


def upgrade(build: bool = False) -> int:
    """短停机升级：停止主控 → 冷备 → 构建/拉起 → 迁移门 → smoke。"""
    subprocess.run(_compose("stop", "server"), check=False)
    code, backup_path = backup(manage_server=False)
    if code:
        print("升级在迁移前终止；当前数据未改变。")
        return code
    if build:
        code = subprocess.run(_compose("build", "server", "migrate"), check=False).returncode
        if code:
            print(f"升级镜像构建失败；备份位于 {backup_path}。")
            return code
    code = subprocess.run(_compose("up", "-d", "--wait", "db"), check=False).returncode
    if code == 0:
        code = subprocess.run(_compose("run", "--rm", "migrate"), check=False).returncode
    if code == 0:
        code = subprocess.run(_compose("up", "-d", "--wait", "--no-deps", "server"), check=False).returncode
    if code:
        print(f"升级启动失败；备份位于 {backup_path}，不要自动执行 Alembic downgrade。")
        return code
    return smoke(BASE_URL)


def down(volumes: bool = False) -> int:
    """停止编排（--volumes 连数据卷一起删：清库重来）。"""
    cmd = _compose("down", *(["-v"] if volumes else []))
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=False).returncode


def status(url: str) -> int:
    """汇总 /livez /readyz /version（urllib 直探宿主端口，不进容器）。"""
    lcode, _ = probe("/livez", url)
    rcode, ready = probe("/readyz", url)
    vcode, ver = probe("/version", url)
    print(f"  livez : {lcode or '连接失败'}")
    print(f"  readyz: {rcode or '连接失败'} {json.dumps(ready.get('checks', ready), ensure_ascii=False)}")
    if vcode == 200:
        schema = ver.get("schema", {})
        print(f"  version: server={ver.get('server')} contracts={ver.get('contracts')} commit={ver.get('commit')}")
        print(f"  schema : {json.dumps(schema, ensure_ascii=False)}")
    else:
        print(f"  version: {vcode or '连接失败'}")
    return 0 if lcode == 200 and rcode == 200 else 1


def smoke(url: str) -> int:
    """v1 冒烟门：readyz 全绿 + /version 三库 revision 与迁移 head 一致即过。

    诚实注明：这不验证采集链路；内置 fixture 全链路冒烟随首启向导（P0-21）集成。
    """
    rcode, ready = probe("/readyz", url)
    if rcode != 200:
        print(f"[!!] readyz={rcode or '连接失败'}：{json.dumps(ready.get('checks', ready), ensure_ascii=False)}")
        return 1
    vcode, ver = probe("/version", url)
    schema = ver.get("schema", {}) if vcode == 200 else {}
    head = schema.get("expected_head")
    bad = {k: v for k, v in schema.items() if k != "expected_head" and v != head}
    if vcode != 200 or not head or bad:
        print(
            f"[!!] 三库 revision 与迁移 head 不一致：head={head}，异常项={json.dumps(bad or ver, ensure_ascii=False)}"
        )
        return 1
    print(f"[OK] 冒烟通过：readyz 全绿，三库 revision 一致（head={head}）")
    print(f"下一步：浏览器打开 {url}/setup 创建首个管理员（空库首启引导）。")
    print("说明：v1 冒烟只验「可服务 + 迁移一致」；全链路 fixture 采集冒烟随首启向导（P0-21）补齐。")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台常为 GBK：不可编码字符降级为 ?，绝不让运维命令因打印崩溃
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="pypctl", description="payipa 主控部署运维命令（从仓库根运行）")
    parser.add_argument("--url", default=BASE_URL, help=f"主控地址（默认 {BASE_URL}，即 compose 的宿主映射）")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("init", help="生成 deploy/.env.compose（各密钥独立随机；已存在拒绝覆盖）")
    sub.add_parser("doctor", help="环境体检：docker/compose/语法/端口/readyz")
    p_up = sub.add_parser("up", help="启动编排（db → migrate → server；--wait 等健康）")
    p_up.add_argument("--build", action="store_true", help="启动前（重新）构建镜像")
    p_up.add_argument("--agents", action="store_true", help="已弃用：Agent 须在管理员建成后用 pypctl agent 接入")
    p_agent = sub.add_parser("agent", help="用节点页签发的一次性入网码启动本机 Agent")
    p_agent.add_argument("--token", required=True, help="一次性入网码（命令输出会脱敏）")
    p_agent.add_argument("--build", action="store_true", help="启动前构建 Agent 镜像")
    p_backup = sub.add_parser("backup", help="冷备三库、对象卷和部署配置")
    p_backup.add_argument("--output", default=None, help="备份目录（默认 backups/<UTC时间>）")
    p_restore = sub.add_parser("restore", help="从 pypctl 备份恢复（覆盖当前数据）")
    p_restore.add_argument("backup_dir", help="含 manifest.json 的备份目录")
    p_restore.add_argument("--confirm", default="", help="危险操作确认词：RESTORE")
    p_upgrade = sub.add_parser("upgrade", help="短停机冷备、迁移并冒烟")
    p_upgrade.add_argument("--build", action="store_true", help="升级时重新构建镜像")
    p_down = sub.add_parser("down", help="停止编排")
    p_down.add_argument("-v", "--volumes", action="store_true", help="连数据卷一起删（清库重来）")
    sub.add_parser("status", help="汇总 /livez /readyz /version")
    sub.add_parser("smoke", help="冒烟门：readyz 全绿 + 三库 revision 一致")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return init()
    if args.cmd == "doctor":
        return doctor(args.url)
    if args.cmd == "up":
        return up(build=args.build, agents=args.agents)
    if args.cmd == "agent":
        return start_agent(args.token, build=args.build)
    if args.cmd == "backup":
        return backup(args.output)[0]
    if args.cmd == "restore":
        return restore(args.backup_dir, args.confirm)
    if args.cmd == "upgrade":
        return upgrade(build=args.build)
    if args.cmd == "down":
        return down(volumes=args.volumes)
    if args.cmd == "status":
        return status(args.url)
    if args.cmd == "smoke":
        return smoke(args.url)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
