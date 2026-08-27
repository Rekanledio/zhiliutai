from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or resolves to a protected address."""


class SourceFetchError(ValueError):
    """Raised when a safe URL cannot be fetched as a static HTML source."""


@dataclass(frozen=True)
class FetchedSource:
    requested_url: str
    final_url: str
    media_type: str
    content: bytes


ResolveHost = Callable[..., list[tuple[object, ...]]]


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_public_url(url: str, *, resolve_host: ResolveHost = socket.getaddrinfo) -> None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise UnsafeUrlError("URL 只允许 http/https")
        if parsed.username or parsed.password:
            raise UnsafeUrlError("URL 不允许包含用户凭据")
        host = parsed.hostname
        if not host:
            raise UnsafeUrlError("URL 缺少主机名")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except UnsafeUrlError:
        raise
    except ValueError as error:
        raise UnsafeUrlError("URL 格式无效") from error

    try:
        addresses = [(host,)] if _is_ip_literal(host) else resolve_host(
            host, port, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError) as error:
        raise UnsafeUrlError("URL 主机无法解析") from error
    if not addresses:
        raise UnsafeUrlError("URL 主机无法解析")
    for result in addresses:
        address = str(result[0] if len(result) == 1 else result[4][0])
        try:
            public = _is_public_address(address)
        except ValueError as error:
            raise UnsafeUrlError("URL 主机地址无效") from error
        if not public:
            raise UnsafeUrlError("URL 主机解析到受保护的网络地址")


class SourceFetcher:
    def __init__(
        self,
        *,
        max_bytes: int = 10_000_000,
        timeout: float = 10.0,
        max_redirects: int = 3,
        resolve_host: ResolveHost = socket.getaddrinfo,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.resolve_host = resolve_host
        self.transport = transport

    def validate(self, url: str) -> None:
        validate_public_url(url, resolve_host=self.resolve_host)

    async def fetch(self, url: str) -> FetchedSource:
        requested_url = url
        current_url = url
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers={"User-Agent": "Zhiliutai/0.3 static-source-fetcher"},
            transport=self.transport,
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                self.validate(current_url)
                try:
                    async with client.stream("GET", current_url) as response:
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location:
                                raise SourceFetchError("网页重定向缺少 Location")
                            if redirect_count >= self.max_redirects:
                                raise SourceFetchError("网页重定向次数超过限制")
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status_code >= 400:
                            raise SourceFetchError(
                                f"网页请求失败：HTTP {response.status_code}"
                            )
                        media_type = response.headers.get("content-type", "").split(
                            ";", 1
                        )[0].strip().lower()
                        if media_type and media_type not in {
                            "text/html",
                            "application/xhtml+xml",
                        }:
                            raise SourceFetchError("URL 来源必须是静态 HTML 页面")
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > self.max_bytes:
                                raise SourceFetchError("网页内容超过大小限制")
                        if not content:
                            raise SourceFetchError("网页内容为空")
                        if not media_type:
                            media_type = "text/html"
                        return FetchedSource(
                            requested_url=requested_url,
                            final_url=current_url,
                            media_type=media_type,
                            content=bytes(content),
                        )
                except SourceFetchError:
                    raise
                except httpx.HTTPError as error:
                    raise SourceFetchError("网页请求失败") from error
        raise SourceFetchError("网页重定向次数超过限制")
