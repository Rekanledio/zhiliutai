import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, sqlite_url_for
from app.providers.rag import (
    OpenAICompatibleRagChatProvider,
    RagProviderUnavailable,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        chat_base_url="https://chat.example/v1",
        chat_model="fake-chat",
        chat_api_key="unit-test-secret",
    )


@pytest.mark.asyncio
async def test_openai_compatible_rag_provider_parses_structured_claims(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer unit-test-secret"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "text": "SQLite 是权威来源。",
                                            "citation_ids": ["C1"],
                                        }
                                    ],
                                    "conflicts": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            },
            request=request,
        )

    provider = OpenAICompatibleRagChatProvider(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    )

    draft = await provider.answer(
        "SQLite 是什么？",
        [{"citation_id": "C1", "title": "测试", "content": "SQLite 证据", "source_type": "markdown"}],
    )

    assert draft.claims[0].citation_ids == ("C1",)
    assert draft.usage.input_tokens == 12
    assert draft.usage.output_tokens == 7
    assert "unit-test-secret" not in repr(draft)


@pytest.mark.asyncio
async def test_rag_provider_hides_upstream_error_body(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=b"upstream body contains unit-test-secret",
            request=request,
        )

    provider = OpenAICompatibleRagChatProvider(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(RagProviderUnavailable) as captured:
        await provider.answer("问题", [])

    assert "unit-test-secret" not in str(captured.value)
