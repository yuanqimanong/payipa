"""错误码枚举（负数）——负数错误码写入 ``requests.state``（smallint）；
``data_*`` 只落成功行（state=3）、不写负数（避免无字段失败行指纹碰撞）。

**第一批"定死"**（SDD §4.4 / 07 / 02 方向）。数值/命名如需调整在 M0 内确认；
新增错误码继续向下取负值、不复用旧值。正数正常态见 :mod:`payipa_contracts.enums` 的 ``RequestState``。

约定：状态列为 smallint，`>=0` 表示正常态、`<0` 表示错误码；达最大重试后定格负码，未达则回 0 重试。
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    """请求/数据失败错误码（负数）。"""

    NETWORK = -1  # 网络失败（连接/DNS/重置等）
    TIMEOUT = -2  # 超时
    ACCESS_PAUSED = -3  # 访问暂停（认证、授权或需人工确认的访问挑战）
    SOFT_FAIL = -4  # 软失败（HTTP 200 但内容为错误页，命中 fail_when）
    PARSE_FAIL = -5  # 解析失败（规则未命中/结构变更）
    NODE_LOST = -6  # 节点失联（心跳超时回收，不计业务重试）


# 人类可读标签（喂 UI / 日志 / 监控）。
ERROR_LABELS: dict[ErrorCode, str] = {
    ErrorCode.NETWORK: "网络失败",
    ErrorCode.TIMEOUT: "超时",
    ErrorCode.ACCESS_PAUSED: "访问暂停",
    ErrorCode.SOFT_FAIL: "软失败",
    ErrorCode.PARSE_FAIL: "解析失败",
    ErrorCode.NODE_LOST: "节点失联",
}


def label(code: int) -> str:
    """返回错误码的中文标签；未知码返回占位。"""
    try:
        return ERROR_LABELS[ErrorCode(code)]
    except ValueError:
        return f"未知错误码({code})"
