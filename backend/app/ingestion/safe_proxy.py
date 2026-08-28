from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlsplit

from app.ingestion.fetcher import (
    ResolveHost,
    UnsafeUrlError,
    resolve_public_addresses,
    validate_public_url,
)


ConnectTarget = Callable[
    [str, int, int, float],
    Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
]


async def open_numeric_target(
    address: str,
    port: int,
    family: int,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(
        asyncio.open_connection(address, port, family=family),
        timeout=timeout,
    )


class SafeProxyError(RuntimeError):
    """A local proxy request could not be handled inside the security policy."""


class LoopbackSafeProxy:
    """Small fail-closed forward proxy used only by one yt-dlp invocation.

    The listener binds to an ephemeral loopback port. Every outbound socket is
    opened against a numeric address returned by one validated DNS resolution,
    so a later resolver answer cannot redirect the connection into a protected
    network. HTTPS remains end-to-end between yt-dlp and the origin; CONNECT
    destinations are still validated before the tunnel is established.
    """

    def __init__(
        self,
        *,
        resolve_host: ResolveHost = socket.getaddrinfo,
        connect_target: ConnectTarget = open_numeric_target,
        timeout: float = 30.0,
        max_header_bytes: int = 65_536,
        max_requests: int = 4_096,
        max_connections: int = 16,
        max_response_bytes: int = 600_000_000,
    ) -> None:
        self.resolve_host = resolve_host
        self.connect_target = connect_target
        self.timeout = max(1.0, timeout)
        self.max_header_bytes = max(1_024, max_header_bytes)
        self.max_requests = max(1, max_requests)
        self.max_response_bytes = max(1, max_response_bytes)
        self._semaphore = asyncio.Semaphore(max(1, max_connections))
        self._request_count = 0
        self._response_bytes = 0
        self._tasks: set[asyncio.Task[object]] = set()
        self._server: asyncio.AbstractServer | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.setblocking(False)
        port = int(self._listener.getsockname()[1])
        self.proxy_url = f"http://127.0.0.1:{port}"
        self._validated_targets: list[str] = []

    @property
    def validated_targets(self) -> tuple[str, ...]:
        return tuple(self._validated_targets)

    async def __aenter__(self) -> LoopbackSafeProxy:
        if self._server is not None:
            raise RuntimeError("安全代理不能重复启动")
        self._server = await asyncio.start_server(self._handle_client, sock=self._listener)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read_header(self, reader: asyncio.StreamReader) -> bytes:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=self.timeout,
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as error:
            raise SafeProxyError("代理请求头无效") from error
        if len(raw) > self.max_header_bytes:
            raise SafeProxyError("代理请求头超过限制")
        return raw

    @staticmethod
    def _parse_header(raw: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
        try:
            lines = raw.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except (UnicodeDecodeError, ValueError) as error:
            raise SafeProxyError("代理请求格式无效") from error
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise SafeProxyError("代理协议版本无效")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            if line[0] in " \t" or ":" not in line:
                raise SafeProxyError("代理请求头无效")
            name, value = line.split(":", 1)
            if (
                not name
                or any(ord(character) < 33 or ord(character) > 126 for character in name)
                or "\x00" in value
            ):
                raise SafeProxyError("代理请求头无效")
            headers.append((name, value.strip()))
        if len(headers) > 100:
            raise SafeProxyError("代理请求头超过限制")
        return method.upper(), target, version, headers

    def _resolve(self, host: str, port: int) -> tuple[tuple[int, str], ...]:
        return resolve_public_addresses(host, port, resolve_host=self.resolve_host)

    async def _connect(
        self,
        host: str,
        port: int,
        *,
        scheme: str,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addresses = self._resolve(host, port)
        last_error: BaseException | None = None
        for family, address in addresses:
            try:
                connected = await self.connect_target(address, port, family, self.timeout)
            except (OSError, TimeoutError) as error:
                last_error = error
                continue
            authority = f"[{host}]" if ":" in host else host
            self._validated_targets.append(f"{scheme}://{authority}:{port}")
            return connected
        raise SafeProxyError("代理目标连接失败") from last_error

    @staticmethod
    def _connect_target(target: str) -> tuple[str, int]:
        try:
            parsed = urlsplit(f"https://{target}")
            host = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise SafeProxyError("CONNECT 目标无效") from error
        if (
            not host
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise SafeProxyError("CONNECT 目标无效")
        return host, port

    def _http_target(self, target: str) -> tuple[str, int, str, str]:
        try:
            parsed = urlsplit(target)
            host = parsed.hostname
            port = parsed.port or 80
        except ValueError as error:
            raise SafeProxyError("HTTP 代理目标无效") from error
        if parsed.scheme.lower() != "http" or not host:
            raise SafeProxyError("HTTP 代理目标无效")
        try:
            validate_public_url(target, resolve_host=self.resolve_host)
        except UnsafeUrlError as error:
            raise SafeProxyError("HTTP 代理目标不安全") from error
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host_header = parsed.netloc
        return host, port, path, host_header

    @staticmethod
    def _http_request(
        method: str,
        path: str,
        version: str,
        host_header: str,
        headers: list[tuple[str, str]],
    ) -> bytes:
        removed = {
            "connection",
            "keep-alive",
            "proxy-authorization",
            "proxy-connection",
            "transfer-encoding",
            "upgrade",
            "host",
        }
        kept = [(name, value) for name, value in headers if name.casefold() not in removed]
        lines = [f"{method} {path} {version}", f"Host: {host_header}", "Connection: close"]
        lines.extend(f"{name}: {value}" for name, value in kept)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    async def _pipe(
        self,
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
        *,
        count_response: bool,
    ) -> None:
        while True:
            chunk = await asyncio.wait_for(source.read(65_536), timeout=self.timeout)
            if not chunk:
                return
            if count_response:
                self._response_bytes += len(chunk)
                if self._response_bytes > self.max_response_bytes:
                    raise SafeProxyError("代理响应超过限制")
            destination.write(chunk)
            await asyncio.wait_for(destination.drain(), timeout=self.timeout)

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = {
            asyncio.create_task(
                self._pipe(client_reader, upstream_writer, count_response=False)
            ),
            asyncio.create_task(
                self._pipe(upstream_reader, client_writer, count_response=True)
            ),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    @staticmethod
    async def _error(writer: asyncio.StreamWriter, status: int, phrase: str) -> None:
        body = b"request rejected"
        writer.write(
            (
                f"HTTP/1.1 {status} {phrase}\r\n"
                "Connection: close\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            + body
        )
        with suppress(OSError, ConnectionError):
            await writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._tasks.add(current)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            async with self._semaphore:
                self._request_count += 1
                if self._request_count > self.max_requests:
                    await self._error(writer, 429, "Too Many Requests")
                    return
                raw = await self._read_header(reader)
                method, target, version, headers = self._parse_header(raw)
                if method == "CONNECT":
                    host, port = self._connect_target(target)
                    upstream_reader, upstream_writer = await self._connect(
                        host, port, scheme="https"
                    )
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    host, port, path, host_header = self._http_target(target)
                    upstream_reader, upstream_writer = await self._connect(
                        host, port, scheme="http"
                    )
                    upstream_writer.write(
                        self._http_request(method, path, version, host_header, headers)
                    )
                    await upstream_writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
        except (SafeProxyError, UnsafeUrlError):
            if not writer.is_closing():
                await self._error(writer, 403, "Forbidden")
        except (OSError, ConnectionError, TimeoutError):
            if not writer.is_closing():
                await self._error(writer, 502, "Bad Gateway")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(OSError, ConnectionError):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(OSError, ConnectionError):
                await writer.wait_closed()
            if current is not None:
                self._tasks.discard(current)
