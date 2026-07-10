"""provider 适配器（11 §定案-2，三类）：统一 `acquire_egress(target) -> Egress` 接口。

- **tunnel（隧道型）**：固定入口 endpoint，代理商侧每连接换 IP（现状常用）；
- **longlived（长效型）**：固定 IP + 时效，到期续订/更换；
- **iplist 型**：调代理商 API 拉一批 IP，本地维护列表 + 轮换 + 健康检查。

每代理商一个 adapter（封装 API/认证/拉取续期差异）；新增代理商 = 加一个 adapter。凭证由上层解密后传入。
真实 provider API 拉取（iplist 的 `_refresh`）留实现期按采购接入——本层给可注入的 `ip_source`（测试/离线用固定列表）。
中转网关（出口数据面 B）作为可独立部署的网络服务延后；本模块只做「选出一个 Egress 描述符」。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

Protocols = ("http", "socks5")


@dataclass(slots=True)
class Egress:
    """一次选出的出口：中转/代理地址 + 协议 + 溯源用 IP 标识。passthrough=直连目标（不选上游）。"""

    protocol: str  # http / socks5
    endpoint: str | None  # host:port（passthrough 时为 None）
    ip: str | None = None  # 溯源用出口 IP 标识（隧道型可能未知）
    provider: str | None = None
    passthrough: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    """provider 契约：给定目标（域/URL），选出一个 Egress。无健康出口时抛 NoEgressAvailable。"""

    kind: str

    def acquire_egress(self, target: str) -> Egress: ...


class NoEgressAvailable(RuntimeError):
    """provider 当前无可用（健康）出口。"""


class TunnelAdapter:
    """隧道型：固定入口 endpoint，每连接换 IP。出口 IP 未知（代理商侧轮换）→ ip=None。"""

    kind = "tunnel"

    def __init__(self, config: dict[str, Any]) -> None:
        self._endpoint = config["endpoint"]  # host:port（认证经 Proxy-Authorization，中转注入）
        self._protocol = config.get("protocol", "http")
        self._name = config.get("name")

    def acquire_egress(self, target: str) -> Egress:  # noqa: ARG002 —— 隧道对 target 无关
        return Egress(protocol=self._protocol, endpoint=self._endpoint, provider=self._name, meta={"kind": "tunnel"})


class LonglivedAdapter:
    """长效型：固定 IP 列表（到期由 provider API 续订/更换，续订留实现期）。轮换取一个健康出口。"""

    kind = "longlived"

    def __init__(self, config: dict[str, Any]) -> None:
        self._name = config.get("name")
        self._protocol = config.get("protocol", "http")
        self._ips: list[str] = list(config.get("ips") or [])  # "host:port" 列表
        self._i = 0

    def acquire_egress(self, target: str, *, healthy: Callable[[str], bool] | None = None) -> Egress:  # noqa: ARG002
        candidates = [ip for ip in self._ips if healthy is None or healthy(ip)]
        if not candidates:
            raise NoEgressAvailable(f"longlived '{self._name}' 无健康出口")
        ip = candidates[self._i % len(candidates)]
        self._i += 1
        host = ip.rsplit(":", 1)[0] if ":" in ip else ip
        return Egress(protocol=self._protocol, endpoint=ip, ip=host, provider=self._name, meta={"kind": "longlived"})


class IpListAdapter:
    """iplist 型：调代理商 API 拉一批 IP（`ip_source`），本地列表 + 轮换 + 健康过滤。

    `ip_source()` 返回 "host:port" 列表；缺省用 config['ips']（离线/测试）。真实 API 拉取按采购接入实现期补。
    """

    kind = "iplist"

    def __init__(self, config: dict[str, Any], *, ip_source: Callable[[], Sequence[str]] | None = None) -> None:
        self._name = config.get("name")
        self._protocol = config.get("protocol", "http")
        self._static: list[str] = list(config.get("ips") or [])
        self._source = ip_source
        self._i = 0

    def _list(self) -> list[str]:
        return list(self._source()) if self._source is not None else self._static

    def acquire_egress(self, target: str, *, healthy: Callable[[str], bool] | None = None) -> Egress:  # noqa: ARG002
        candidates = [ip for ip in self._list() if healthy is None or healthy(ip)]
        if not candidates:
            raise NoEgressAvailable(f"iplist '{self._name}' 无健康出口")
        ip = candidates[self._i % len(candidates)]
        self._i += 1
        host = ip.rsplit(":", 1)[0] if ":" in ip else ip
        return Egress(protocol=self._protocol, endpoint=ip, ip=host, provider=self._name, meta={"kind": "iplist"})


_ADAPTERS: dict[str, Callable[[dict[str, Any]], ProviderAdapter]] = {
    "tunnel": TunnelAdapter,
    "longlived": LonglivedAdapter,
    "iplist": IpListAdapter,
}


def build_adapter(kind: str, config: dict[str, Any]) -> ProviderAdapter:
    """按 kind 建 adapter；未知 kind 抛 ValueError。config 为解密后的 api_config 明文。"""
    factory = _ADAPTERS.get(kind)
    if factory is None:
        raise ValueError(f"未知 proxy provider kind：{kind!r}（支持 {sorted(_ADAPTERS)}）")
    return factory(config)


PASSTHROUGH = Egress(protocol="http", endpoint=None, passthrough=True, meta={"kind": "passthrough"})


__all__ = [
    "PASSTHROUGH",
    "Egress",
    "IpListAdapter",
    "LonglivedAdapter",
    "NoEgressAvailable",
    "ProviderAdapter",
    "TunnelAdapter",
    "build_adapter",
]
