"""pyp-agent 命令行入口。

一行接入：``pyp-agent join --server <URL> --token <一次性 join token>``（[01 §2.4]）。
M0 骨架：解析参数并说明；WS 接入循环于 M1 落地（见 :mod:`pyp_agent.conn`）。
"""

from __future__ import annotations

import argparse

from pyp_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyp-agent", description="payipa 子节点采集端")
    parser.add_argument("--version", action="version", version=f"pyp-agent {__version__}")
    sub = parser.add_subparsers(dest="command")

    join = sub.add_parser("join", help="出站接入主控（注册后进入心跳/领任务循环）")
    join.add_argument("--server", required=True, help="主控 URL，如 https://pyp.example.com")
    join.add_argument("--token", required=True, help="一次性 join token")
    join.add_argument("--slots", type=int, default=None, help="并发槽 N（默认按机器规格）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "join":
        print(
            f"[pyp-agent {__version__}] join server={args.server} slots={args.slots} "
            "(M0 骨架：出站 WS 注册→心跳→领任务→回报 将于 M1 落地)"
        )
        # M1: import anyio; anyio.run(AgentConnection(args.server, args.token, args.slots).run)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
