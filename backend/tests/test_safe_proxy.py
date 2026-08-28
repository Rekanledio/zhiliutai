import asyncio
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.ingestion.fetcher import UnsafeUrlError, resolve_public_addresses
from app.providers.video import (
    CommandResult,
    LoopbackYtDlpNetworkExecutor,
    VideoDownloadOptions,
)


def _answer(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


def test_public_resolution_rejects_mixed_or_rebinding_addresses() -> None:
    def mixed(_host: str, port: int, **_kwargs):
        return [_answer("93.184.216.34", port), _answer("127.0.0.1", port)]

    with pytest.raises(UnsafeUrlError, match="受保护"):
        resolve_public_addresses("video.example", 443, resolve_host=mixed)


@pytest.mark.asyncio
async def test_loopback_proxy_validates_and_pins_each_connect_without_outbound_network(
    tmp_path: Path,
) -> None:
    resolutions = 0
    connected: list[tuple[str, int, int]] = []

    def resolve(_host: str, port: int, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [_answer(address, port)]

    async def connect(address: str, port: int, family: int, _timeout: float):
        connected.append((address, port, family))
        raise OSError("synthetic connection stop")

    executor = LoopbackYtDlpNetworkExecutor(
        resolve_host=resolve,
        connect_target=connect,
        timeout=1,
    )

    class Runner:
        responses: list[bytes] = []

        async def run(self, args, *, cwd: Path, timeout: float, env=None) -> CommandResult:
            del cwd, timeout, env
            proxy_url = args[args.index("--proxy") + 1]
            parsed = urlsplit(proxy_url)
            assert parsed.hostname == "127.0.0.1"
            assert parsed.port is not None
            for _ in range(2):
                reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
                writer.write(
                    b"CONNECT video.example:443 HTTP/1.1\r\n"
                    b"Host: video.example:443\r\n\r\n"
                )
                await writer.drain()
                self.responses.append(await reader.read(256))
                writer.close()
                await writer.wait_closed()
            return CommandResult(0)

    runner = Runner()
    result = await executor.execute(
        runner,
        ["yt-dlp", "--proxy", executor.proxy_url, "--", "https://video.example/watch"],
        cwd=tmp_path,
        timeout=2,
        env={},
        requested_url="https://video.example/watch",
        options=VideoDownloadOptions(
            max_bytes=100,
            max_duration_ms=1_000,
            timeout_seconds=2,
            max_redirects=2,
        ),
    )

    assert result.network_policy_enforced is True
    assert resolutions == 2
    assert connected == [("93.184.216.34", 443, socket.AF_INET)]
    assert all(response.startswith(b"HTTP/1.1 403") for response in runner.responses)
