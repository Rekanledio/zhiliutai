import json
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.providers.video import FakeASRProvider, FakeAudioExtractor, FakeVideoSourceProvider
from app.video.types import SubtitleTrack, TranscriptSegment, VideoSourceMetadata
from conftest import wait_for_job


def _install_provider(client: TestClient, provider: FakeVideoSourceProvider) -> None:
    service = client.app.state.stage2_service.video_service
    source_fetcher = client.app.state.stage2_service.source_fetcher

    def resolve_public(_host: str, port: int, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    source_fetcher.resolve_host = resolve_public
    service.video_provider = provider
    service.url_validator = source_fetcher.validate


def _subtitle(text: str = "字幕第一段") -> SubtitleTrack:
    return SubtitleTrack(
        content=(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.500\n"
            + text
            + "\n\n"
            "00:00:02.000 --> 00:00:03.000\n字幕第二段\n"
        ).encode(),
        format="vtt",
        language="zh-Hans",
        provider="fake-subtitles",
        tool_version="fixture-v1",
    )


def test_video_url_boundary_rejects_credentials_sensitive_options_and_local_paths(client) -> None:
    for body in (
        {"url": "https://user:password@example.test/video"},
        {"url": "https://@example.test/video"},
        {"url": "https://example.test/video?token=synthetic-secret"},
        {"url": "file:///tmp/video.mp4"},
        {"url": "C:\\Users\\Lenovo\\video.mp4"},
        {"url": "https://example.test/video", "cookies": "not-accepted"},
        {"url": "https://example.test/video", "downloader_args": ["--proxy"]},
    ):
        response = client.post("/api/sources/video", json=body)
        assert response.status_code == 422
        assert "synthetic-secret" not in response.text
        assert "C:\\Users" not in response.text


def test_video_submission_is_idempotent_and_does_not_fetch_at_api_boundary(client) -> None:
    provider = FakeVideoSourceProvider(subtitles=(_subtitle(),))
    _install_provider(client, provider)
    body = {
        "url": "https://example.test/video",
        "idempotency_key": "video-key",
    }
    first = client.post("/api/sources/video", json=body)
    assert first.status_code == 202, first.text
    repeated = client.post("/api/sources/video", json=body)
    assert repeated.status_code == 202
    assert repeated.json() == {**first.json(), "deduplicated": True}
    assert provider.calls == 0

    conflict = client.post(
        "/api/sources/video",
        json={"url": "https://example.test/other", "idempotency_key": "video-key"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_subtitles_are_preferred_and_stored_as_pending_without_indexing(client, settings) -> None:
    provider = FakeVideoSourceProvider(
        metadata=VideoSourceMetadata(
            source_url="https://example.test/video",
            duration_ms=3_000,
            title="合成字幕视频",
            provider="fake-video",
            tool_version="fixture-v1",
        ),
        subtitles=(_subtitle(),),
        media=None,
    )
    _install_provider(client, provider)
    submitted = client.post(
        "/api/sources/video",
        json={"url": "https://example.test/video", "language": "zh-Hans"},
    )
    assert submitted.status_code == 202, submitted.text
    job = wait_for_job(client, submitted.json()["job_id"])
    assert job["stage"] == "pending_review"
    item = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert item["status"] == "pending_review"
    assert item["current_content_version_id"] is None
    assert item["has_pending_review"] is True
    assert "字幕第一段" in item["body"]
    assert "asr" not in json.dumps(job, ensure_ascii=False).lower()

    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT artifact_type, retention_policy FROM source_artifacts "
            "WHERE knowledge_item_id = ? ORDER BY created_at",
            (submitted.json()["item_id"],),
        ).fetchall()
        current, pending, chunks = connection.execute(
            "SELECT current_content_version_id, pending_content_version_id, "
            "(SELECT count(*) FROM chunks WHERE knowledge_item_id = knowledge_items.id) "
            "FROM knowledge_items WHERE id = ?",
            (submitted.json()["item_id"],),
        ).fetchone()
    assert [row[0] for row in rows] == ["video_source", "video_subtitle", "video_transcript"]
    assert rows[1][1] == rows[2][1] == "permanent"
    assert current is None
    assert pending
    assert chunks == 0


def test_video_without_subtitles_reports_asr_required_without_calling_asr(client) -> None:
    provider = FakeVideoSourceProvider(media=b"synthetic-video", subtitles=())
    _install_provider(client, provider)
    # The default ASR is intentionally not configured in the test app.  The
    # video service must not pretend that FFmpeg/ASR succeeded.
    service = client.app.state.stage2_service.video_service
    service.asr_provider = None
    service.audio_extractor = None
    submitted = client.post("/api/sources/video", json={"url": "https://example.test/no-captions"})
    assert submitted.status_code == 202
    job = wait_for_job(client, submitted.json()["job_id"])
    assert job["stage"] == "asr_required"
    assert job["result"]["status"] == "asr_required"
    assert job["result"]["asr_called"] is False


def test_video_without_subtitles_uses_controlled_audio_and_asr_then_cleans_media(
    client, settings
) -> None:
    provider = FakeVideoSourceProvider(
        metadata=VideoSourceMetadata(
            source_url="https://example.test/asr",
            duration_ms=4_000,
            title="无字幕合成视频",
        ),
        media=b"synthetic-video-media",
    )
    _install_provider(client, provider)
    service = client.app.state.stage2_service.video_service
    audio = FakeAudioExtractor(b"synthetic-audio")
    asr = FakeASRProvider(
        [
            TranscriptSegment(
                start_ms=0,
                end_ms=1_000,
                duration_ms=4_000,
                text="ASR 第一段",
                language="zh",
            ),
            TranscriptSegment(
                start_ms=1_000,
                end_ms=2_000,
                duration_ms=4_000,
                text="ASR 第二段",
                language="zh",
            ),
        ]
    )
    service.audio_extractor = audio
    service.asr_provider = asr
    submitted = client.post("/api/sources/video", json={"url": "https://example.test/asr"})
    assert submitted.status_code == 202
    job = wait_for_job(client, submitted.json()["job_id"])
    assert job["result"]["status"] == "asr_complete"
    assert audio.calls == 1
    assert asr.calls == 1
    item = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert "ASR 第一段" in item["body"]
    assert item["status"] == "pending_review"

    with sqlite3.connect(settings.database_path) as connection:
        media = connection.execute(
            "SELECT relative_path, cleanup_state FROM source_artifacts "
            "WHERE knowledge_item_id = ? AND artifact_type = 'video_media'",
            (submitted.json()["item_id"],),
        ).fetchone()
        transcript = connection.execute(
            "SELECT relative_path FROM source_artifacts "
            "WHERE knowledge_item_id = ? AND artifact_type = 'video_transcript'",
            (submitted.json()["item_id"],),
        ).fetchone()
    assert media is not None and media[1] == "deleted"
    assert not (settings.artifact_root / media[0]).exists()
    assert transcript is not None and (settings.artifact_root / transcript[0]).is_file()


def test_video_timeout_failure_can_be_retried_without_changing_published_current(
    client, settings
) -> None:
    class FlakyProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = FakeVideoSourceProvider(subtitles=(_subtitle("可重试字幕"),))

        async def acquire(self, url, *, destination, options):
            self.calls += 1
            if self.calls == 1:
                from app.providers.video import VideoProviderError

                raise VideoProviderError("视频来源获取超时")
            return await self.delegate.acquire(url, destination=destination, options=options)

    provider = FlakyProvider()
    provider.delegate.metadata = VideoSourceMetadata(
        source_url="https://example.test/flaky", duration_ms=3_000
    )
    _install_provider(client, provider)
    submitted = client.post("/api/sources/video", json={"url": "https://example.test/flaky"})
    assert submitted.status_code == 202
    failed = wait_for_job(client, submitted.json()["job_id"], expected="failed")
    assert failed["error"]["message"] == "视频来源获取超时"
    item_before = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert item_before["status"] == "failed"
    retried = client.post(f"/api/jobs/{submitted.json()['job_id']}/retry")
    assert retried.status_code == 200
    succeeded = wait_for_job(client, submitted.json()["job_id"])
    assert succeeded["stage"] == "pending_review"
    assert provider.calls == 2

    # No current version existed on this new item; a failed attempt never
    # created a partial pending version.
    with sqlite3.connect(settings.database_path) as connection:
        current, pending = connection.execute(
            "SELECT current_content_version_id, pending_content_version_id "
            "FROM knowledge_items WHERE id = ?",
            (submitted.json()["item_id"],),
        ).fetchone()
    assert current is None
    assert pending


@pytest.mark.parametrize(
    "content",
    [
        "WEBVTT\n\n00:00:02.000 --> 00:00:03.000\nsecond\n\n00:00:01.000 --> 00:00:02.000\nfirst",
        "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n<script>alert(1)</script>",
        "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\njavascript:alert(1)",
        "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;",
    ],
)
def test_malformed_subtitles_fail_without_changing_current(client, content) -> None:
    provider = FakeVideoSourceProvider(
        metadata=VideoSourceMetadata(source_url="https://example.test/video", duration_ms=3_000),
        subtitles=(SubtitleTrack(content=content.encode(), format="vtt", language="zh"),),
    )
    _install_provider(client, provider)
    submitted = client.post("/api/sources/video", json={"url": "https://example.test/bad"})
    assert submitted.status_code == 202
    job = wait_for_job(client, submitted.json()["job_id"], expected="failed")
    assert job["error"]["type"] in {"SubtitleParseError", "VideoTextError"}
    assert "<script>" not in json.dumps(job)
    item = client.get(f"/api/items/{submitted.json()['item_id']}").json()
    assert item["status"] == "failed"
    assert item["has_pending_review"] is False
