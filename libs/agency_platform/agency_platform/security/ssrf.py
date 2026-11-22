"""SSRF-safe outbound fetching for agent tools.

Agents fetch URLs that come from search results or user prompts -- attacker
controlled. Without this, "summarise http://169.254.169.254/latest/meta-data"
or "http://model-gateway:8000/..." would let a prompt read cloud credentials or
hit internal services. Every hop (including redirects) is validated.

Residual risk: DNS rebinding between validation and connect. The Kubernetes
NetworkPolicy (deploy/base/networkpolicy.yaml) blocks private/metadata ranges at
the network layer as the second line of defence.
"""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_PORTS = {None, 80, 443}
_MAX_REDIRECTS = 4
_USER_AGENT = "AutonomousAgencyBot/1.0 (+research assistant)"


class UnsafeURLError(ValueError):
    pass


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status_code: int
    content_type: str
    text: str
    truncated: bool


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not (addr.is_multicast or addr.is_reserved)


async def assert_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme}")
    if not parts.hostname:
        raise UnsafeURLError("missing host")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL not allowed")
    if parts.port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"port not allowed: {parts.port}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host: {parts.hostname}") from exc
    for info in infos:
        if not _is_public(info[4][0]):
            raise UnsafeURLError(f"host resolves to non-public address: {parts.hostname}")


async def safe_fetch(
    url: str,
    *,
    client: httpx.AsyncClient,
    max_bytes: int = 2_000_000,
    timeout_s: float = 15.0,
) -> FetchedPage:
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_public_url(current)
        async with client.stream(
            "GET", current, headers={"User-Agent": _USER_AGENT}, timeout=timeout_s, follow_redirects=False
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeURLError("redirect without location")
                current = urljoin(current, location)
                continue
            content_type = resp.headers.get("content-type", "")
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    truncated = True
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
            return FetchedPage(current, resp.status_code, content_type, text, truncated)
    raise UnsafeURLError("too many redirects")
