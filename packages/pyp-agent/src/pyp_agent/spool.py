"""有界结果 spool：发送前原子落盘，收到主控 ResultAck 后删除。"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path

from payipa_contracts import ResultReport

_DEFAULT_LIMIT = 100 * 1024 * 1024


def _limit_bytes() -> int:
    try:
        mb = int(os.environ.get("PYP_AGENT_SPOOL_MB", "100"))
    except ValueError:
        return _DEFAULT_LIMIT
    return mb * 1024 * 1024 if mb > 0 else _DEFAULT_LIMIT


def _dir(state_dir: Path) -> Path:
    return state_dir / "spool"


def _key(req_id: str, attempt: int) -> str:
    return hashlib.sha256(f"{req_id}:{attempt}".encode()).hexdigest()


def _path(state_dir: Path, req_id: str, attempt: int) -> Path:
    return _dir(state_dir) / f"{_key(req_id, attempt)}.json"


def put(state_dir: Path, report: ResultReport) -> None:
    """幂等写一份结果；目录总量超过上限时拒绝新结果，避免填满磁盘。"""
    directory = _dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = _path(state_dir, report.result.req_id, report.result.attempt)
    payload = report.model_dump_json().encode()
    existing = target.stat().st_size if target.exists() else 0
    used = sum(p.stat().st_size for p in directory.glob("*.json"))
    if used - existing + len(payload) > _limit_bytes():
        raise RuntimeError("agent result spool capacity exceeded")
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)


def pending(state_dir: Path) -> list[ResultReport]:
    """读取可重发结果；损坏文件保留为 .bad，避免静默丢数据或反复阻塞启动。"""
    directory = _dir(state_dir)
    if not directory.exists():
        return []
    reports: list[ResultReport] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime):
        try:
            reports.append(ResultReport.model_validate_json(path.read_bytes()))
        except Exception:  # noqa: BLE001
            with contextlib.suppress(OSError):
                path.replace(path.with_suffix(".bad"))
    return reports


def ack(state_dir: Path, req_id: str, attempt: int) -> bool:
    try:
        _path(state_dir, req_id, attempt).unlink()
    except FileNotFoundError:
        return False
    return True
