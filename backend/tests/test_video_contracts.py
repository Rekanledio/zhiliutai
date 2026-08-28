import pytest
from pydantic import ValidationError

from app.providers.video import (
    ASRProvider,
    DeterministicASRProvider,
    DeterministicVisionProvider,
    FakeASRProvider,
    VisionProvider,
)
from app.schemas.video import VideoSourceRequest
from app.video.types import (
    Chapter,
    Keyframe,
    ProcessingManifest,
    TranscriptSegment,
    VideoSourceMetadata,
    VisualEvent,
)


@pytest.mark.parametrize("model", [TranscriptSegment, Chapter, Keyframe, VisualEvent])
@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [(-1, 1), (0, 0), (2, 1), (0, 1.5)],
)
def test_video_intervals_require_strict_non_empty_integer_milliseconds(
    model: type[object], start_ms: object, end_ms: object
) -> None:
    common = {
        TranscriptSegment: {"text": "text"},
        Chapter: {"title": "chapter"},
        Keyframe: {"keyframe_id": "frame-1"},
        VisualEvent: {"summary": "scene"},
    }[model]
    with pytest.raises(ValidationError):
        model(start_ms=start_ms, end_ms=end_ms, **common)  # type: ignore[call-arg]


def test_timed_models_accept_zero_start_and_validate_an_isolated_duration() -> None:
    segment = TranscriptSegment(
        start_ms=0,
        end_ms=1000,
        duration_ms=1000,
        text="确定性字幕",
    )
    assert segment.start_ms == 0
    assert segment.end_ms == 1000
    with pytest.raises(ValidationError):
        TranscriptSegment(start_ms=0, end_ms=1001, duration_ms=1000, text="超出时长")


def test_processing_manifest_validates_all_nested_spans_against_source_duration() -> None:
    source = VideoSourceMetadata(
        source_url="https://example.com/video",
        duration_ms=10_000,
    )
    manifest = ProcessingManifest(
        source=source,
        transcript=[TranscriptSegment(start_ms=0, end_ms=2_000, text="ok")],
        chapters=[Chapter(start_ms=2_000, end_ms=4_000, title="第一章")],
        keyframes=[Keyframe(start_ms=4_000, end_ms=4_001, keyframe_id="kf-1")],
        visual_events=[VisualEvent(start_ms=4_000, end_ms=5_000, summary="slide")],
    )
    assert manifest.source_metadata.duration_ms == 10_000
    assert manifest.source is manifest.source_metadata

    with pytest.raises(ValidationError, match=r"transcript_segments\[0\]"):
        ProcessingManifest(
            source_metadata=source,
            transcript_segments=[
                TranscriptSegment(start_ms=9_000, end_ms=10_001, text="超出来源时长")
            ],
        )


def test_video_source_request_accepts_http_alias_and_rejects_credentials() -> None:
    request = VideoSourceRequest(source_url="https://example.com/video", enable_vision=True)
    assert request.url == request.source_url == "https://example.com/video"
    with pytest.raises(ValidationError):
        VideoSourceRequest(url="https://user:password@example.com/video")
    with pytest.raises(ValidationError):
        VideoSourceRequest(url="file:///tmp/video.mp4")


@pytest.mark.asyncio
async def test_deterministic_asr_provider_is_offline_and_repeatable() -> None:
    provider = DeterministicASRProvider()
    assert isinstance(provider, ASRProvider)
    first = await provider.transcribe(b"synthetic-audio", language="zh", duration_ms=2_000)
    second = await provider.transcribe(b"synthetic-audio", language="zh", duration_ms=2_000)
    assert first == second
    assert first[0].end_ms == 2_000
    assert first[0].text.startswith("fake transcript ")
    assert await provider.transcribe(b"empty", duration_ms=0) == []


@pytest.mark.asyncio
async def test_fake_asr_provider_returns_supplied_segments() -> None:
    segment = TranscriptSegment(start_ms=0, end_ms=500, text="fixture")
    provider = FakeASRProvider([segment])
    assert await provider.transcribe(b"ignored", duration_ms=500) == [segment]


@pytest.mark.asyncio
async def test_deterministic_vision_provider_is_offline_and_repeatable() -> None:
    provider = DeterministicVisionProvider()
    assert isinstance(provider, VisionProvider)
    keyframe = Keyframe(start_ms=1_000, end_ms=2_000, keyframe_id="kf-1")
    first = await provider.analyze(b"synthetic-image", keyframe=keyframe)
    second = await provider.analyze(b"synthetic-image", keyframe=keyframe)
    assert first == second
    assert first[0].start_ms == keyframe.start_ms
    assert first[0].end_ms == keyframe.end_ms
    assert first[0].keyframe_ids == ["kf-1"]


def test_video_contracts_do_not_accept_unknown_secret_fields() -> None:
    with pytest.raises(ValidationError):
        VideoSourceMetadata(
            source_url="https://example.com/video",
            api_key="synthetic-secret",  # type: ignore[call-arg]
        )
    assert "synthetic-secret" not in repr(VideoSourceRequest(url="https://example.com/video"))
