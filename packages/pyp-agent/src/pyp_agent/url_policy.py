"""Agent 出网边界：协议、域白名单与 DNS 地址分类校验。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import anyio
from niquests.packages.urllib3.contrib.resolver import ProtocolResolver
from niquests.packages.urllib3.contrib.resolver._async import (
    AsyncBaseResolver,
    AsyncResolverDescription,
)


class URLPolicyError(ValueError):
    """目标 URL 超出任务授权边界；错误文本不回显完整 URL。"""


def _normalized_host(value: str) -> str:
    raw = value.strip().rstrip(".").lower()
    try:
        return raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLPolicyError("target hostname is invalid") from exc


async def resolve_host(host: str, port: int) -> set[str]:
    """解析主机全部地址；独立函数便于测试和后续接入受控 DNS。"""

    def _resolve() -> set[str]:
        return {item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}

    try:
        return await anyio.to_thread.run_sync(_resolve)
    except socket.gaierror as exc:
        raise URLPolicyError("target hostname could not be resolved") from exc


def _domain_allowed(host: str, allowed_domains: list[str]) -> bool:
    for item in allowed_domains:
        allowed = _normalized_host(item)
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def _validate_addresses(addresses: set[str]) -> None:
    if not addresses:
        raise URLPolicyError("target hostname has no addresses")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise URLPolicyError("target DNS returned an invalid address") from exc
        if not address.is_global:
            raise URLPolicyError("target resolves to a non-public address")


class PublicAddressResolver(AsyncBaseResolver):
    """niquests resolver that validates the exact records handed to the connector."""

    implementation = "payipa-public-only"
    protocol = ProtocolResolver.SYSTEM

    def __init__(self, allowed_domains: list[str], resolver=None) -> None:
        super().__init__(None, None)
        self._allowed_domains = list(allowed_domains)
        self._resolver = resolver or AsyncResolverDescription(ProtocolResolver.SYSTEM).new()

    def is_available(self) -> bool:
        return self._resolver.is_available()

    def recycle(self):
        return PublicAddressResolver(self._allowed_domains)

    async def close(self) -> None:
        await self._resolver.close()

    async def getaddrinfo(
        self,
        host: bytes | str | None,
        port: str | int | None,
        family: socket.AddressFamily,
        type: socket.SocketKind,
        proto: int = 0,
        flags: int = 0,
        *,
        quic_upgrade_via_dns_rr: bool = False,
    ):
        if host is None:
            raise URLPolicyError("target hostname is missing")
        hostname = _normalized_host(host.decode("ascii") if isinstance(host, bytes) else host)
        if not _domain_allowed(hostname, self._allowed_domains):
            raise URLPolicyError("target domain is outside the task allowlist")
        records = await self._resolver.getaddrinfo(
            host,
            port,
            family,
            type,
            proto,
            flags,
            quic_upgrade_via_dns_rr=quic_upgrade_via_dns_rr,
        )
        _validate_addresses({str(record[4][0]).split("%", 1)[0] for record in records})
        return records


async def browser_pinned_hosts(url: str, allowed_domains: list[str]) -> dict[str, str]:
    """Resolve explicit Browser hosts once so Chromium connects to those validated addresses."""
    target = urlsplit(url).hostname
    hosts = {_normalized_host(value) for value in allowed_domains}
    if target:
        hosts.add(_normalized_host(target))
    pins: dict[str, str] = {}
    for host in sorted(hosts):
        addresses = await resolve_host(host, 443)
        _validate_addresses(addresses)
        ordered = sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version != 4, value))
        pins[host] = ordered[0]
    return pins


async def validate_url(url: str, allowed_domains: list[str]) -> None:
    """逐跳验证 URL；任一 DNS 结果非公网即拒绝，防混合解析与常见 DNS rebinding。"""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLPolicyError("only http and https targets are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("target URL must not contain userinfo")
    if not parsed.hostname:
        raise URLPolicyError("target URL has no hostname")
    host = _normalized_host(parsed.hostname)
    if not allowed_domains or not _domain_allowed(host, allowed_domains):
        raise URLPolicyError("target domain is outside the task allowlist")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLPolicyError("target port is invalid") from exc
    addresses = await resolve_host(host, port)
    _validate_addresses(addresses)
