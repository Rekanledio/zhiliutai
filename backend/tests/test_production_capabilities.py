from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, sqlite_url_for
from app.main import create_app
from app.providers.models import ProviderNotConfigured
from app.providers.capabilities import (
    FasterWhisperASRProvider,
    FfmpegKeyframeSampler,
    OpenAICompatibleVisionProvider,
)
from app.providers.video import CommandResult, VideoProviderError, VideoSecurityError
from app.rag.reranking import SentenceTransformersReranker
from app.rag.types import RetrievedChunk
from app.video.types import Keyframe, VideoSourceMetadata


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        workflow_checkpoint_path=tmp_path / "checkpoints.sqlite",
        backup_root=tmp_path / "backups",
        **overrides,
    )


@pytest.mark.asyncio
async def test_faster_whisper_falls_back_to_cpu_without_exposing_loader_error(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    class CpuModel:
        def transcribe(self, _audio: object, **_: object):
            segments = [SimpleNamespace(start=0.0, end=1.25, text="  测试 转录  ")]
            return segments, SimpleNamespace(language="zh")

    def loader(_model: str, **kwargs: object) -> object:
        device = str(kwargs["device"])
        compute_type = str(kwargs["compute_type"])
        calls.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("synthetic secret cuda failure")
        return CpuModel()

    provider = FasterWhisperASRProvider(
        _settings(
            tmp_path,
            asr_provider="faster-whisper",
            asr_model="medium",
            asr_cache_path=tmp_path / "models",
        ),
        loader=loader,
    )

    segments = await provider.transcribe(b"synthetic-wave", duration_ms=2_000)

    assert calls == [("cuda", "int8_float16"), ("cpu", "int8")]
    assert [segment.text for segment in segments] == ["测试 转录"]
    assert segments[0].end_ms == 1_250


@pytest.mark.asyncio
async def test_vision_sends_only_inline_image_and_parses_strict_json(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        body = json.loads(request.content)
        captured["image_url"] = body["messages"][0]["content"][1]["image_url"]["url"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"event_type": "slide", "summary": "一张本地测试幻灯片"},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleVisionProvider(
        _settings(
            tmp_path,
            vision_base_url="https://api.deepseek.com",
            vision_model="deepseek-v4-flash-vision-exp",
            vision_api_key="synthetic-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    keyframe = Keyframe(
        keyframe_id="frame-1",
        start_ms=0,
        end_ms=1_000,
        duration_ms=2_000,
    )

    events = await provider.analyze(b"\x89PNG\r\n\x1a\nsynthetic", keyframe=keyframe)

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer synthetic-key"
    assert str(captured["image_url"]).startswith("data:image/png;base64,")
    assert "file:" not in str(captured["image_url"])
    assert events[0].event_type == "slide"
    assert events[0].keyframe_ids == ["frame-1"]


@pytest.mark.asyncio
async def test_vision_rejects_unrecognized_bytes_before_network(tmp_path: Path) -> None:
    provider = OpenAICompatibleVisionProvider(
        _settings(
            tmp_path,
            vision_base_url="https://api.deepseek.com",
            vision_model="deepseek-v4-flash-vision-exp",
        ),
        transport=httpx.MockTransport(lambda _: pytest.fail("network must not be called")),
    )
    keyframe = Keyframe(keyframe_id="frame-1", start_ms=0, end_ms=1)
    with pytest.raises(VideoProviderError, match="格式不受支持"):
        await provider.analyze(b"not-an-image", keyframe=keyframe)


def test_vision_rejects_endpoint_credentials(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfigured, match="endpoint is invalid"):
        OpenAICompatibleVisionProvider(
            _settings(
                tmp_path,
                vision_base_url="https://user:secret@api.deepseek.com",
                vision_model="deepseek-v4-flash-vision-exp",
            )
        )


class _FrameRunner:
    async def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: object = None,
    ) -> CommandResult:
        del args, timeout, env
        (cwd / "vision-frame-000001.webp").write_bytes(b"RIFFxxxxWEBPsynthetic")
        return CommandResult(0)


@pytest.mark.asyncio
async def test_ffmpeg_sampler_stays_inside_artifact_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    media = root / "video.mp4"
    media.write_bytes(b"synthetic")
    workdir = root / "work"
    sampler = FfmpegKeyframeSampler(root, runner=_FrameRunner())
    metadata = VideoSourceMetadata(
        source_url="https://example.test/video",
        duration_ms=45_000,
    )

    frames = await sampler.detect(
        media,
        metadata=metadata,
        destination=workdir,
        max_keyframes=2,
    )

    assert len(frames) == 1
    assert frames[0].keyframe.start_ms == 0
    with pytest.raises(VideoSecurityError, match="目录越界"):
        await sampler.detect(
            media,
            metadata=metadata,
            destination=tmp_path / "outside",
            max_keyframes=2,
        )


@pytest.mark.asyncio
async def test_sentence_transformers_reranker_is_lazy_and_maps_scores(tmp_path: Path) -> None:
    loads: list[dict[str, object]] = []

    class CrossEncoder:
        def predict(self, pairs: list[tuple[str, str]], **_: object) -> list[float]:
            assert len(pairs) == 2
            return [0.2, 0.9]

    def loader(_model: str, **kwargs: object) -> object:
        loads.append(kwargs)
        return CrossEncoder()

    reranker = SentenceTransformersReranker(
        "BAAI/bge-reranker-v2-m3",
        device="cpu",
        cache_path=tmp_path / "models",
        loader=loader,
    )
    chunks = [
        RetrievedChunk(
            chunk_id="a",
            knowledge_item_id="i1",
            content_version_id="v1",
            item_title="甲",
            version_no=1,
            source_type="text",
            content="甲",
            source_locator="items/a.md",
        ),
        RetrievedChunk(
            chunk_id="b",
            knowledge_item_id="i2",
            content_version_id="v2",
            item_title="乙",
            version_no=1,
            source_type="text",
            content="乙",
            source_locator="items/b.md",
        ),
    ]

    scores = await reranker.rerank("问题", chunks)

    assert scores == {"a": 0.2, "b": 0.9}
    assert loads[0]["device"] == "cpu"
    assert loads[0]["local_files_only"] is True


def test_create_app_wires_configured_capabilities_without_loading_models(tmp_path: Path) -> None:
    app = create_app(
        _settings(
            tmp_path,
            asr_provider="faster-whisper",
            asr_model="medium",
            vision_base_url="https://api.deepseek.com",
            vision_model="deepseek-v4-flash-vision-exp",
            vision_api_key="synthetic-key",
            reranker_provider="sentence-transformers",
            reranker_model="BAAI/bge-reranker-v2-m3",
            reranker_cache_path=tmp_path / "reranker",
        ),
        start_background=False,
        serve_frontend=False,
    )

    assert isinstance(app.state.video_service.asr_provider, FasterWhisperASRProvider)
    assert isinstance(app.state.video_service.vision_provider, OpenAICompatibleVisionProvider)
    assert isinstance(app.state.video_service.scene_detector, FfmpegKeyframeSampler)
    assert isinstance(app.state.rag_retriever.reranker, SentenceTransformersReranker)
