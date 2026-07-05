"""契约协议版本号与握手校验。

agent 连接时上报其契约版本，主控据此接受/拒绝（过旧或过新）并给出升级提示（09 升级策略）。
**破坏性变更必须 +1**（并跑向后兼容/快照测试，SDD §3.6）。新增可选字段（带默认值）向后兼容、无需升版本。
"""

from __future__ import annotations

# 整数协议号。破坏性变更时 +1。
CONTRACT_VERSION: int = 1

# 主控可接受的最低 agent 协议版本（低于此拒连并提示升级）。
MIN_SUPPORTED_CONTRACT_VERSION: int = 1


def is_compatible(peer_version: int) -> bool:
    """对端契约版本是否与本端兼容。"""
    return MIN_SUPPORTED_CONTRACT_VERSION <= peer_version <= CONTRACT_VERSION


def assert_compatible(peer_version: int) -> None:
    """不兼容则抛 ValueError（调用方转为握手拒绝帧/升级提示）。"""
    if not is_compatible(peer_version):
        raise ValueError(
            f"契约版本不兼容：对端 ={peer_version}，本端支持 [{MIN_SUPPORTED_CONTRACT_VERSION}, {CONTRACT_VERSION}]"
        )
