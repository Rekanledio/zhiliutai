import asyncio
import json
import socket
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ingestion.fetcher import SourceFetcher, UnsafeUrlError, validate_public_url
from app.providers.video import (
    AudioExtractionOptions,
    CommandResult,
    DeterministicYtDlpNetworkExecutor,
    FakeOCRProvider,
    FakeSceneDetector,
    FakeVideoSourceProvider,
    FakeVisionProvider,
    FfmpegAudioExtractor,
    FrameSample,
    VideoDownloadOptions,
    VideoDownloadResult,
    VideoProviderError,
    YtDlpDownloader,
)
from app.video.types import Keyframe, SubtitleTrack, VideoSourceMetadata
from app.obsidian.markdown import ObsidianVault
from conftest import wait_for_job


def _install_provider(client: TestClient, provider: object) -> None:
    service = client.app.state.stage2_service.video_service
    source_fetcher = client.app.state.stage2_service.source_fetcher

    def resolve_public(_host: str, port: int, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    source_fetcher.resolve_host = resolve_public
    service.video_provider = provider
    service.url_validator = source_fetcher.validate


def _subtitle(text: str, *, language: str = "zh-Hans") -> SubtitleTrack:
    return SubtitleTrack(
        content=(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.500\n"
            + text
            + "\n\n"
            "00:00:02.000 --> 00:00:03.000\n字幕第二段\n"
        ).encode(),
        format="vtt",
        language=language,
        provider="fixture-subtitles",
        tool_version="fixture-v1",
    )


def _metadata(url: str, *, kind: str = "other", duration_ms: int = 4_000) -> VideoSourceMetadata:
    return VideoSourceMetadata(
        source_url=url,
        duration_ms=duration_ms,
        title="阶段 5 视频 fixture",
        video_kind=kind,
        provider="fixture-video",
        tool_version="fixture-v1",
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_video_url_policy_rejects_protected_ipv4_ipv6_and_documentation_ranges(address: str) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolve(_host: str, port: int, **_kwargs):
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://video.example/watch", resolve_host=resolve)


@pytest.mark.asyncio
async def test_video_url_policy_revalidates_every_redirect_and_dns_rebinding_style_input() -> None:
    calls = 0

    def resolve(_host: str, port: int, **_kwargs):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://video.example/second-hop"},
            request=request,
        )

    fetcher = SourceFetcher(
        resolve_host=resolve,
        transport=httpx.MockTransport(handler),
        max_redirects=3,
    )
    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch("https://video.example/first-hop")
    assert calls == 2


@pytest.mark.asyncio
async def test_video_provider_must_report_a_changed_final_url_as_a_validated_chain(client) -> None:
    class UnreportedRedirectProvider:
        async def acquire(self, url, *, destination, options):
            return VideoDownloadResult(
                _metadata("https://video.example/final"),
                None,
                (),
                (),
            )

    _install_provider(client, UnreportedRedirectProvider())
    submitted = client.post("/api/sources/video", json={"url": "https://video.example/start"})
    job = wait_for_job(client, submitted.json()["job_id"], expected="failed")
    assert job["error"]["code"] == "job_failed"
    assert "重定向链" in job["error"]["message"]


@pytest.mark.asyncio
async def test_ytdlp_adapter_uses_closed_args_and_deterministic_runner(tmp_path: Path) -> None:
    class Runner:
        def __init__(self) -> None:
            self.args: list[str] = []

        async def run(self, args, *, cwd: Path, timeout: float, env=None) -> CommandResult:
            self.args = list(args)
            Path(cwd, "fixture-video.mp4").write_bytes(b"video")
            Path(cwd, "fixture-video.zh-Hans.vtt").write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n字幕\n",
                encoding="utf-8",
            )
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": "fixture-id",
                        "title": "离线视频",
                        "duration": 1.0,
                        "ext": "mp4",
                        "webpage_url": "https://video.example/watch",
                    }
                ).encode(),
            )

    runner = Runner()
    seen: list[str] = []

    def validate(url: str) -> None:
        seen.append(url)

    executor = DeterministicYtDlpNetworkExecutor()
    downloader = YtDlpDownloader(
        tmp_path,
        runner=runner,
        url_validator=validate,
        network_executor=executor,
    )
    result = await downloader.download(
        "https://video.example/watch",
        destination=tmp_path / ".video-work",
        options=VideoDownloadOptions(
            max_bytes=100,
            max_duration_ms=10_000,
            timeout_seconds=2,
            max_redirects=2,
            subtitle_languages=("zh-Hans",),
        ),
    )
    assert result.media_path is not None and result.media_path.name == "fixture-video.mp4"
    assert len(result.subtitle_tracks) == 1
    assert seen == ["https://video.example/watch", "https://video.example/watch"]
    assert runner.args == [
        "yt-dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--no-cache-dir",
        "--no-call-home",
        "--proxy",
        executor.proxy_url,
        "--downloader",
        "native",
        "--restrict-filenames",
        "--max-downloads",
        "1",
        "--max-filesize",
        "100",
        "--socket-timeout",
        "2",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "zh-Hans",
        "--sub-format",
        "vtt/srt/best",
        "--print-json",
        "--paths",
        f"home:{(tmp_path / '.video-work').resolve()}",
        "--output",
        "zhiliutai-%(id)s.%(ext)s",
        "--",
        "https://video.example/watch",
    ]
    assert executor.calls[0]["requested_url"] == "https://video.example/watch"
    environment = executor.calls[0]["env"]
    state_home = Path(environment["HOME"])
    config_home = Path(environment["XDG_CONFIG_HOME"])
    assert state_home.name == ".ytdlp-state"
    assert config_home.name == "config"
    assert config_home.parent.name == ".ytdlp-state"
    assert config_home.parent == state_home
    assert config_home == state_home / "config"
    assert not any(
        name.casefold() in {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "cookiefile",
            "ytdlp_config",
            "ytdlp_plugin_dirs",
        }
        for name in environment
    )
    assert all(flag not in runner.args for flag in {
        "--cookies",
        "--cookies-from-browser",
        "--netrc-location",
        "--config-locations",
        "--exec",
        "--exec-before-download",
    })
    assert runner.args[-1] == "https://video.example/watch"


@pytest.mark.asyncio
async def test_ytdlp_adapter_accepts_bounded_stop_only_with_completed_output(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self, *, write_media: bool) -> None:
            self.write_media = write_media

        async def run(self, args, *, cwd: Path, timeout: float, env=None) -> CommandResult:
            del args, timeout, env
            if self.write_media:
                Path(cwd, "fixture-video.mp4").write_bytes(b"video")
            return CommandResult(
                101,
                json.dumps(
                    {
                        "id": "fixture-id",
                        "title": "离线视频",
                        "duration": 1.0,
                        "ext": "mp4",
                        "webpage_url": "https://video.example/watch",
                    }
                ).encode(),
            )

    options = VideoDownloadOptions(
        max_bytes=100,
        max_duration_ms=10_000,
        timeout_seconds=2,
        max_redirects=2,
    )
    successful = YtDlpDownloader(
        tmp_path,
        runner=Runner(write_media=True),
        url_validator=lambda _url: None,
        network_executor=DeterministicYtDlpNetworkExecutor(),
    )
    result = await successful.download(
        "https://video.example/watch",
        destination=tmp_path / "successful",
        options=options,
    )
    assert result.media_path is not None
    assert result.media_path.read_bytes() == b"video"

    missing_output = YtDlpDownloader(
        tmp_path,
        runner=Runner(write_media=False),
        url_validator=lambda _url: None,
        network_executor=DeterministicYtDlpNetworkExecutor(),
    )
    with pytest.raises(VideoProviderError, match="来源获取失败"):
        await missing_output.download(
            "https://video.example/watch",
            destination=tmp_path / "missing-output",
            options=options,
        )


@pytest.mark.asyncio
async def test_ytdlp_adapter_uses_default_loopback_network_boundary(tmp_path: Path) -> None:
    class Runner:
        calls = 0
        args: list[str] = []

        async def run(self, args, *, cwd: Path, timeout: float, env=None) -> CommandResult:
            self.calls += 1
            self.args = list(args)
            Path(cwd, "fixture-video.mp4").write_bytes(b"video")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": "fixture-id",
                        "title": "离线视频",
                        "duration": 1.0,
                        "ext": "mp4",
                        "webpage_url": "https://video.example/watch",
                    }
                ).encode(),
            )

    runner = Runner()
    downloader = YtDlpDownloader(tmp_path, runner=runner, url_validator=lambda _url: None)
    result = await downloader.download(
        "https://video.example/watch",
        destination=tmp_path / ".video-work",
        options=VideoDownloadOptions(
            max_bytes=100,
            max_duration_ms=10_000,
            timeout_seconds=2,
            max_redirects=2,
        ),
    )
    assert runner.calls == 1
    assert result.network_policy_enforced is True
    proxy_index = runner.args.index("--proxy")
    assert runner.args[proxy_index + 1].startswith("http://127.0.0.1:")
    assert runner.args[runner.args.index("--downloader") + 1] == "native"


@pytest.mark.asyncio
async def test_ffmpeg_audio_adapter_uses_fixed_args_and_injected_runner(tmp_path: Path) -> None:
    class Runner:
        def __init__(self) -> None:
            self.args: list[str] = []

        async def run(self, args, *, cwd: Path, timeout: float, env=None) -> CommandResult:
            self.args = list(args)
            Path(cwd, "audio.wav").write_bytes(b"synthetic-wav")
            return CommandResult(0)

    source = tmp_path / "input.mp4"
    source.write_bytes(b"synthetic-video")
    destination = tmp_path / ".video-work"
    runner = Runner()
    extractor = FfmpegAudioExtractor(
        tmp_path,
        executable="fixture-ffmpeg",
        runner=runner,
    )
    output = await extractor.extract(
        source,
        destination=destination,
        options=AudioExtractionOptions(max_bytes=100, timeout_seconds=1),
    )
    assert output == destination.resolve() / "audio.wav"
    assert runner.args == [
        "fixture-ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source.resolve()),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(output),
    ]


def test_video_cancel_preserves_item_and_cleans_unpersisted_output(client, settings) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider:
        async def acquire(self, url, *, destination, options):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return VideoDownloadResult(_metadata(url), None, ())

    provider = BlockingProvider()
    _install_provider(client, provider)
    submitted = client.post("/api/sources/video", json={"url": "https://video.example/cancel"})
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]
    assert started.wait(2)
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    release.set()
    job = wait_for_job(client, job_id, expected="cancelled")
    assert job["stage"] == "cancelled"
    item = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert item["status"] == "processing"
    assert item["has_pending_review"] is False
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM source_artifacts WHERE knowledge_item_id = ?",
            (submitted.json()["item_id"],),
        ).fetchone()[0] == 1


def test_video_conditional_vision_deduplicates_frames_and_skips_interviews(client) -> None:
    frames = [
        FrameSample(
            Keyframe(keyframe_id="slide-1", start_ms=0, end_ms=2_000, duration_ms=4_000),
            b"same-frame",
        ),
        FrameSample(
            Keyframe(keyframe_id="slide-2", start_ms=2_000, end_ms=4_000, duration_ms=4_000),
            b"same-frame",
        ),
    ]
    slideshow_provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/slides", kind="slideshow"),
        subtitles=(_subtitle("幻灯片字幕"),),
        media=b"synthetic-media",
    )
    _install_provider(client, slideshow_provider)
    service = client.app.state.stage2_service.video_service
    detector = FakeSceneDetector(frames)
    vision = FakeVisionProvider()
    ocr = FakeOCRProvider("投影标题")
    service.scene_detector = detector
    service.vision_provider = vision
    service.ocr_provider = ocr
    submitted = client.post(
        "/api/sources/video",
        json={"url": "https://video.example/slides", "enable_vision": True},
    )
    job = wait_for_job(client, submitted.json()["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["visual_event_count"] == 2
    assert detector.calls == 1
    assert vision.calls == 1
    assert ocr.calls == 1
    item = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert "视觉观察" in item["body"]

    interview_provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/interview", kind="interview"),
        subtitles=(_subtitle("访谈字幕"),),
        media=b"synthetic-media",
    )
    _install_provider(client, interview_provider)
    interview_detector = FakeSceneDetector(frames)
    interview_vision = FakeVisionProvider()
    service.scene_detector = interview_detector
    service.vision_provider = interview_vision
    service.ocr_provider = ocr
    interview = client.post(
        "/api/sources/video",
        json={"url": "https://video.example/interview", "enable_vision": True},
    )
    interview_job = wait_for_job(client, interview.json()["job_id"])
    assert interview_job["result"]["visual_event_count"] == 0
    assert interview_detector.calls == 0
    assert interview_vision.calls == 0
    assert ocr.calls == 1


def test_published_video_keyframe_citation_opens_verified_artifact(client) -> None:
    provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/keyframe", kind="tutorial"),
        subtitles=(_subtitle("关键帧对应字幕"),),
        media=b"synthetic-media",
    )
    _install_provider(client, provider)
    service = client.app.state.stage2_service.video_service
    service.scene_detector = FakeSceneDetector(
        [
            FrameSample(
                Keyframe(
                    keyframe_id="frame-1",
                    start_ms=0,
                    end_ms=2_000,
                    duration_ms=4_000,
                ),
                b"unique-keyframe-image",
            )
        ]
    )
    service.vision_provider = FakeVisionProvider()
    submitted = client.post(
        "/api/sources/video",
        json={"url": "https://video.example/keyframe", "enable_vision": True},
    )
    assert submitted.status_code == 202, submitted.text
    job = wait_for_job(client, submitted.json()["job_id"])
    assert job["result"]["visual_event_count"] == 1
    item_id = submitted.json()["item_id"]
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text

    search = client.post(
        "/api/search",
        json={"query": "fake visual observation", "source_types": ["video"]},
    )
    assert search.status_code == 200, search.text
    result = search.json()["results"][0]
    citation = result["citation"]
    assert citation["locator_status"] == "exact"
    assert citation["locator"]["kind"] == "video_keyframe"
    assert citation["locator"]["keyframe_ids"] == ["frame-1"]
    assert citation["target"]["kind"] == "artifact"
    assert citation["target"]["start_ms"] == 0
    frame = client.get(f"/api/artifacts/{citation['target']['artifact_id']}")
    assert frame.status_code == 200, frame.text
    assert frame.content == b"unique-keyframe-image"
    locator = client.get(
        f"/api/artifacts/{citation['target']['artifact_id']}/locator",
        params={
            "start_ms": 0,
            "end_ms": 2_000,
            "keyframe_id": "frame-1",
        },
    )
    assert locator.status_code == 200, locator.text
    assert locator.json() == {
        "kind": "keyframe",
        "artifact_id": citation["target"]["artifact_id"],
        "keyframe_id": "frame-1",
        "start_ms": 0,
        "end_ms": 2_000,
        "media_type": "image/webp",
    }


def test_video_until_expiry_cleanup_is_explicit_and_hash_verified(client, settings) -> None:
    settings.video_media_retention_policy = "until_expiry"
    settings.video_media_retention_days = 1
    provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/retained"),
        subtitles=(_subtitle("保留媒体字幕"),),
        media=b"retained-media",
    )
    _install_provider(client, provider)
    submitted = client.post("/api/sources/video", json={"url": "https://video.example/retained"})
    wait_for_job(client, submitted.json()["job_id"])
    with sqlite3.connect(settings.database_path) as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM source_artifacts "
            "WHERE knowledge_item_id = ? AND artifact_type = 'video_media'",
            (submitted.json()["item_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE source_artifacts SET cleanup_state = 'due' "
            "WHERE knowledge_item_id = ? AND artifact_type = 'video_media'",
            (submitted.json()["item_id"],),
        )
        connection.commit()
    media_path = settings.artifact_root / relative_path
    assert media_path.is_file()
    cleaned = client.post("/api/video/cleanup")
    assert cleaned.status_code == 200
    assert cleaned.json()["deleted"] == 1
    assert not media_path.exists()


def _publish_video(client: TestClient, url: str, provider: object) -> dict[str, object]:
    _install_provider(client, provider)
    submitted = client.post("/api/sources/video", json={"url": url})
    assert submitted.status_code == 202, submitted.text
    wait_for_job(client, submitted.json()["job_id"])
    item_id = submitted.json()["item_id"]
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    return published.json()


def test_published_video_indexes_only_after_review_and_builds_exact_citation(client) -> None:
    provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/published"),
        subtitles=(_subtitle("可引用字幕"),),
        media=None,
    )
    published = _publish_video(client, "https://video.example/published", provider)
    assert published["status"] == "published"
    assert published["pending_content_version_id"] is None
    assert published["current_content_version_id"]
    search = client.post(
        "/api/search",
        json={"query": "字幕第二段", "source_types": ["video"]},
    )
    assert search.status_code == 200, search.text
    citation = search.json()["results"][0]["citation"]
    assert citation["locator_status"] == "exact"
    assert citation["locator"]["kind"] == "video"
    assert citation["target"]["kind"] == "artifact"
    transcript_id = citation["target"]["artifact_id"]
    head = client.get(f"/api/artifacts/{transcript_id}")
    assert head.status_code == 200, head.text
    transcript = client.get(f"/api/artifacts/{transcript_id}")
    assert transcript.status_code == 200
    assert transcript.content.startswith(b"WEBVTT")
    locator = client.get(
        f"/api/artifacts/{transcript_id}/locator",
        params={"start_ms": 2_000, "end_ms": 3_000},
    )
    assert locator.status_code == 200, locator.text
    assert locator.json()["kind"] == "transcript"
    assert locator.json()["text"] == "字幕第二段"
    assert locator.json()["segments"] == [
        {"start_ms": 2_000, "end_ms": 3_000, "text": "字幕第二段", "language": "zh-Hans"}
    ]


def test_video_reprocess_keeps_old_current_until_new_pending_is_published(client, settings) -> None:
    first_provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/reprocess"),
        subtitles=(_subtitle("旧版本字幕"),),
        media=None,
    )
    first = _publish_video(client, "https://video.example/reprocess", first_provider)
    item_id = first["id"]
    old_current = first["current_content_version_id"]
    reprocess_provider = FakeVideoSourceProvider(
        metadata=_metadata("https://video.example/reprocess"),
        subtitles=(_subtitle("新版本字幕"),),
        media=None,
    )
    _install_provider(client, reprocess_provider)
    queued = client.post(f"/api/items/{item_id}/reprocess")
    assert queued.status_code == 200
    wait_for_job(client, queued.json()["job_id"])
    pending = client.get(f"/api/items/{item_id}").json()
    assert pending["status"] == "published"
    assert pending["current_content_version_id"] == old_current
    assert pending["pending_content_version_id"]
    assert "新版本字幕" in pending["body"]
    with sqlite3.connect(settings.database_path) as connection:
        item_hash, current_hash = connection.execute(
            "SELECT knowledge_items.content_hash, content_versions.content_hash "
            "FROM knowledge_items JOIN content_versions "
            "ON content_versions.id = knowledge_items.current_content_version_id "
            "WHERE knowledge_items.id = ?",
            (item_id,),
        ).fetchone()
    assert item_hash == current_hash
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    final = client.post(f"/api/items/{item_id}/publish")
    assert final.status_code == 200, final.text
    assert final.json()["current_content_version_id"] != old_current
    assert final.json()["pending_content_version_id"] is None
    new_search = client.post("/api/search", json={"query": "新版本字幕"})
    assert new_search.status_code == 200
    assert new_search.json()["results"][0]["content_version_id"] == final.json()["current_content_version_id"]


def test_video_publish_failures_compensate_vault_sqlite_and_qdrant_and_retry(
    client, settings, monkeypatch
) -> None:
    first = _publish_video(
        client,
        "https://video.example/consistent",
        FakeVideoSourceProvider(
            metadata=_metadata("https://video.example/consistent"),
            subtitles=(_subtitle("一致性旧字幕"),),
            media=None,
        ),
    )
    item_id = first["id"]
    old_current = first["current_content_version_id"]
    relative_path = first["note_relative_path"]
    vault_path = settings.managed_vault_root / relative_path
    old_note = vault_path.read_bytes()
    original_embedding = client.app.state.stage2_service.embedding_provider
    original_vector_store = client.app.state.stage2_service.vector_store

    def publish_safely():
        transport = client._transport
        previous_raise = transport.raise_server_exceptions
        transport.raise_server_exceptions = False
        try:
            return client.post(f"/api/items/{item_id}/publish")
        finally:
            transport.raise_server_exceptions = previous_raise

    def queue_pending(text: str) -> dict[str, object]:
        _install_provider(
            client,
            FakeVideoSourceProvider(
                metadata=_metadata("https://video.example/consistent"),
                subtitles=(_subtitle(text),),
                media=None,
            ),
        )
        queued = client.post(f"/api/items/{item_id}/reprocess")
        assert queued.status_code == 200, queued.text
        job = wait_for_job(client, queued.json()["job_id"])
        assert job["state"] == "succeeded"
        assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
        return client.get(f"/api/items/{item_id}").json()

    pending = queue_pending("一致性 Embedding 故障字幕")
    pending_id = pending["pending_content_version_id"]
    assert pending["current_content_version_id"] == old_current

    class FailingEmbedding:
        model = "fake-embedding"
        version = "failure-v1"

        async def embed(self, _texts: list[str]) -> list[list[float]]:
            raise RuntimeError("synthetic-key must not be returned")

    client.app.state.stage2_service.embedding_provider = FailingEmbedding()
    failed_embedding = publish_safely()
    assert failed_embedding.status_code == 500
    assert failed_embedding.json()["error"]["code"] == "internal_error"
    assert "synthetic-key" not in failed_embedding.text
    after_embedding_failure = client.get(f"/api/items/{item_id}").json()
    assert after_embedding_failure["current_content_version_id"] == old_current
    assert after_embedding_failure["pending_content_version_id"] == pending_id
    assert vault_path.read_bytes() == old_note
    old_search = client.post(
        "/api/search", json={"query": "一致性旧字幕", "source_types": ["video"]}
    )
    assert old_search.status_code == 200
    assert old_search.json()["results"][0]["content_version_id"] == old_current
    client.app.state.stage2_service.embedding_provider = original_embedding

    class UpsertThenFail:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.deleted_versions: list[str] = []
            self.upserted_versions: list[str] = []

        def upsert(self, records) -> None:
            self.upserted_versions.extend(record.content_version_id for record in records)
            self.wrapped.upsert(records)
            raise RuntimeError("synthetic-qdrant-response")

        def delete_version(self, version_id: str) -> None:
            self.deleted_versions.append(version_id)
            self.wrapped.delete_version(version_id)

        def delete_item_except_version(self, item_id: str, version_id: str) -> None:
            self.wrapped.delete_item_except_version(item_id, version_id)

    failing_vector_store = UpsertThenFail(original_vector_store)
    client.app.state.stage2_service.vector_store = failing_vector_store
    failed_qdrant = publish_safely()
    assert failed_qdrant.status_code == 500
    assert "synthetic-qdrant-response" not in failed_qdrant.text
    assert failing_vector_store.upserted_versions
    assert set(failing_vector_store.upserted_versions) <= set(failing_vector_store.deleted_versions)
    after_qdrant_failure = client.get(f"/api/items/{item_id}").json()
    assert after_qdrant_failure["current_content_version_id"] == old_current
    assert after_qdrant_failure["pending_content_version_id"] == pending_id
    assert vault_path.read_bytes() == old_note
    old_search = client.post(
        "/api/search", json={"query": "一致性旧字幕", "source_types": ["video"]}
    )
    assert old_search.status_code == 200
    assert old_search.json()["results"][0]["content_version_id"] == old_current
    leaked_search = client.post(
        "/api/search", json={"query": "一致性 Embedding 故障字幕", "source_types": ["video"]}
    )
    assert leaked_search.status_code == 200
    assert all(
        result["content_version_id"] not in failing_vector_store.upserted_versions
        for result in leaked_search.json()["results"]
    )

    client.app.state.stage2_service.vector_store = original_vector_store
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    new_current = published.json()["current_content_version_id"]
    assert new_current != old_current
    assert published.json()["pending_content_version_id"] is None
    assert "一致性 Embedding 故障字幕" in vault_path.read_text(encoding="utf-8")

    queue_pending("一致性 Vault 校验故障字幕")
    vault_read_original = ObsidianVault.read

    def fail_vault_verify(self, path):
        note = vault_read_original(self, path)
        return type(note)(metadata=note.metadata, body="synthetic-vault-mismatch")

    monkeypatch.setattr(ObsidianVault, "read", fail_vault_verify)
    failed_validation = publish_safely()
    monkeypatch.setattr(ObsidianVault, "read", vault_read_original)
    assert failed_validation.status_code == 500
    assert "synthetic-vault-mismatch" not in failed_validation.text
    after_validation_failure = client.get(f"/api/items/{item_id}").json()
    assert after_validation_failure["current_content_version_id"] == new_current
    assert after_validation_failure["pending_content_version_id"]
    assert "一致性 Embedding 故障字幕" in vault_path.read_text(encoding="utf-8")

    retry_after_validation = client.post(f"/api/items/{item_id}/publish")
    assert retry_after_validation.status_code == 200, retry_after_validation.text
    validation_current = retry_after_validation.json()["current_content_version_id"]
    assert validation_current != new_current

    queue_pending("一致性 Vault 故障字幕")
    vault_failure_original = ObsidianVault.commit_staged
    vault_failure_raised = False

    def fail_vault_commit(self, staged) -> None:
        nonlocal vault_failure_raised
        vault_failure_original(self, staged)
        if not vault_failure_raised:
            vault_failure_raised = True
            raise OSError("synthetic-vault-write")

    monkeypatch.setattr(ObsidianVault, "commit_staged", fail_vault_commit)
    failed_vault = publish_safely()
    assert failed_vault.status_code == 500
    assert "synthetic-vault-write" not in failed_vault.text
    after_vault_failure = client.get(f"/api/items/{item_id}").json()
    assert after_vault_failure["current_content_version_id"] == validation_current
    assert after_vault_failure["pending_content_version_id"]
    assert "一致性 Vault 校验故障字幕" in vault_path.read_text(encoding="utf-8")
    assert not list(vault_path.parent.glob(".*.tmp"))
    monkeypatch.setattr(ObsidianVault, "commit_staged", vault_failure_original)
    retried = client.post(f"/api/items/{item_id}/publish")
    assert retried.status_code == 200, retried.text
    assert retried.json()["current_content_version_id"] != validation_current
    assert retried.json()["pending_content_version_id"] is None
