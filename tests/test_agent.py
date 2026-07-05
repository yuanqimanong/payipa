"""pyp-agent 骨架冒烟：CLI 可用；运行期不拉入 payipa-core（红线，import-linter 静态强制）。"""

from __future__ import annotations

import subprocess
import sys


def test_cli_parser_join() -> None:
    from pyp_agent.cli import build_parser

    args = build_parser().parse_args(["join", "--server", "https://x", "--token", "t", "--slots", "8"])
    assert args.command == "join"
    assert args.server == "https://x"
    assert args.slots == 8


def test_cli_main_join_runs() -> None:
    from pyp_agent.cli import main

    assert main(["join", "--server", "https://x", "--token", "t"]) == 0


def test_agent_does_not_import_core() -> None:
    # 全新解释器只导入 agent 各模块，断言未拉入 payipa（core）
    code = (
        "import pyp_agent.cli, pyp_agent.conn, pyp_agent.rules, "
        "pyp_agent.upload, pyp_agent.sandbox_client, sys; "
        "bad=[m for m in sys.modules if m=='payipa' or m.startswith('payipa.')]; "
        "assert not bad, bad; print('clean')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "clean" in r.stdout
