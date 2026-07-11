"""节点身份持久化（P0-07/08）：node_uuid 首启生成、node_token 入网后保存。

身份文件 ``<state_dir>/identity.json``；容器重建只要挂了持久卷，节点身份就不变
（hostname 只是显示属性）。写入走临时文件 + 原子替换；权限尽力收紧到 0600（Windows 忽略）。
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path

_FILE = "identity.json"


def state_dir(path: str | None = None) -> Path:
    """身份目录：默认 ``~/.pyp-agent``（容器里映射持久卷）。"""
    return Path(path) if path else Path.home() / ".pyp-agent"


def load_state(dir_: Path) -> dict:
    """读身份文件；不存在/损坏返回空 dict（损坏时按首启处理，不炸进程）。"""
    f = dir_ / _FILE
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError, OSError, json.JSONDecodeError:
        return {}


def save_state(dir_: Path, **kv) -> dict:
    """合并写身份文件（None 值忽略；临时文件 + 原子替换）。返回合并后内容。"""
    dir_.mkdir(parents=True, exist_ok=True)
    data = load_state(dir_) | {k: v for k, v in kv.items() if v is not None}
    f = dir_ / _FILE
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    with contextlib.suppress(OSError):
        os.chmod(f, 0o600)  # 含长期凭证；POSIX 收紧权限，Windows 尽力而为
    return data


def node_id(dir_: Path) -> str:
    """取稳定节点 id；首启生成 uuid4 并持久化（容器 hostname 易变，不作身份）。"""
    nid = load_state(dir_).get("node_uuid")
    if not nid:
        nid = uuid.uuid4().hex
        save_state(dir_, node_uuid=nid)
    return str(nid)
