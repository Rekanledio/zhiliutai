import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text
from app.ingestion.fetcher import UnsafeUrlError, validate_public_url
from app.providers.rag import (
    AnswerClaim,
    AnswerDraft,
    OpenAICompatibleRagChatProvider,
    RagProviderUnavailable,
)
from app.services.artifacts import ArtifactStore
from app.obsidian.markdown import ObsidianVault

from conftest import wait_for_job


SYNTHETIC_KEY = "sk-synthetic-api-key-123456"
SYNTHETIC_VAULT_PATH = r"D:\Vault\Private\note.md"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Notes/安全.md", "Notes/安全.md"),
        (r"D:\Vault\Private\note.md", None),
        ("D:/Vault/Private/note.md", None),
        (r"\\server\share\note.md", None),
        ("//server/share/note.md", None),
        ("/absolute/note.md", None),
        ("Notes/../note.md", None),
        ("Notes/./note.md", None),
        ("Notes//note.md", None),
        ("C:note.md", None),
        (".", None),
    ],
)
def test_safe_relative_path_rejects_windows_absolute_and_noncanonical_targets(
    value: str, expected: str | None
) -> None:
    assert safe_relative_path(value) == expected


def test_artifact_and_vault_reject_unsafe_targets(tmp_path: Path) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    vault = ObsidianVault(tmp_path / "vault", "Notes")
    unsafe = [r"D:\Vault\Private\note.md", "D:/Vault/note.md", "../note.md"]
    for target in unsafe:
        assert artifact_store.exists(target) is False
        with pytest.raises(ValueError):
            vault.resolve(target)


@pytest.mark.parametrize(
    "url",
    [
        "https://93.184.216.34/article?api_key=synthetic-key",
        "https://93.184.216.34/article#token=synthetic-token",
    ],
)
def test_public_url_rejects_credentials_in_query_or_fragment(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


def test_text_redaction_handles_json_secrets_and_quoted_windows_paths() -> None:
    value = '{"api_key":"synthetic-value","path":"D:\\Vault Root\\note.md"}'
    redacted = redact_sensitive_text(value)
    assert "synthetic-value" not in redacted
    assert r"D:\Vault Root\note.md" not in redacted


def test_obsidian_open_rejects_a_missing_temporary_note(client: TestClient) -> None:
    item_id = _publish_text(client, "临时 Obsidian 引用目标。")
    item = client.get(f"/api/items/{item_id}").json()
    note_path = client.app.state.settings.managed_vault_root / item["note_relative_path"]
    note_path.unlink()

    response = client.post(f"/api/obsidian/open/{item_id}")

    assert response.status_code in {404, 409}
    assert "uri" not in response.text
    assert str(note_path) not in response.text


def _publish_text(client: TestClient, content: str) -> str:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert submitted.status_code == 202, submitted.text
    item_id = submitted.json()["item_id"]
    wait_for_job(client, submitted.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{item_id}/publish").status_code == 200
    return item_id


def test_validation_error_does_not_echo_raw_input_or_sensitive_values(
    client: TestClient,
) -> None:
    raw_query = SYNTHETIC_KEY + " " + SYNTHETIC_VAULT_PATH + " " + ("x" * 2_000)
    response = client.post("/api/search", json={"query": raw_query})

    assert response.status_code == 422
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert SYNTHETIC_KEY not in serialized
    assert SYNTHETIC_VAULT_PATH not in serialized
    assert '"input"' not in serialized


class SensitiveRagProvider:
    provider = "synthetic-rag"
    model = "synthetic-rag-v1"
    prompt_version = "synthetic-rag-test-v1"

    async def answer(self, _query, _evidence):
        return AnswerDraft(
            claims=(
                AnswerClaim(
                    f"上游返回 {SYNTHETIC_KEY}，路径是 {SYNTHETIC_VAULT_PATH}",
                    ("C1",),
                ),
            ),
            conflicts=(f"冲突响应：{SYNTHETIC_KEY}",),
        )

    async def rewrite_query(self, query: str) -> str:
        return query


def test_model_run_citation_and_sse_redact_sensitive_provider_output(
    client: TestClient,
) -> None:
    _publish_text(client, "SQLite 证据提供可追溯的合成来源。")
    service = client.app.state.question_answer_service
    service.chat_provider = SensitiveRagProvider()

    response = client.post("/api/chat/stream", json={"query": "SQLite 证据"})

    assert response.status_code == 200, response.text
    assert SYNTHETIC_KEY not in response.text
    assert SYNTHETIC_VAULT_PATH not in response.text
    assert "[REDACTED]" in response.text
    runs, citations = client.portal.call(_read_audit_rows, client.app.state.session_factory)
    assert runs
    assert citations
    audit_text = json.dumps(
        [
            {
                "input": run.input_json,
                "output": run.output_json,
                "error": run.error_json,
            }
            for run in runs
        ]
        + [
            {
                "excerpt": citation.excerpt,
                "locator": citation.source_locator,
                "retrieval": citation.retrieval_json,
            }
            for citation in citations
        ],
        ensure_ascii=False,
    )
    assert SYNTHETIC_KEY not in audit_text
    assert SYNTHETIC_VAULT_PATH not in audit_text


class LeakyErrorRagProvider:
    provider = "synthetic-rag"
    model = "synthetic-rag-v1"
    prompt_version = "synthetic-rag-test-v1"

    async def answer(self, _query, _evidence):
        raise RagProviderUnavailable(f"upstream body {SYNTHETIC_KEY} {SYNTHETIC_VAULT_PATH}")


def test_provider_error_response_redacts_upstream_sensitive_text(client: TestClient) -> None:
    _publish_text(client, "SQLite 证据提供可追溯的合成来源。")
    client.app.state.question_answer_service.chat_provider = LeakyErrorRagProvider()

    response = client.post("/api/chat/stream", json={"query": "SQLite 证据"})

    assert response.status_code == 503
    assert SYNTHETIC_KEY not in response.text
    assert SYNTHETIC_VAULT_PATH not in response.text


@pytest.mark.asyncio
async def test_upstream_sensitive_response_is_not_exposed_by_rag_provider(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=f"upstream {SYNTHETIC_KEY} {SYNTHETIC_VAULT_PATH}".encode(),
            request=request,
        )

    provider = OpenAICompatibleRagChatProvider(
        settings.model_copy(
            update={
                "chat_base_url": "https://chat.synthetic.test",
                "chat_model": "synthetic-chat",
                "chat_api_key": SYNTHETIC_KEY,
            }
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RagProviderUnavailable) as raised:
        await provider.answer("合成问题", [{"citation_id": "C1", "title": "标题", "content": "证据", "source_type": "markdown"}])
    assert SYNTHETIC_KEY not in str(raised.value)
    assert SYNTHETIC_VAULT_PATH not in str(raised.value)


async def _read_audit_rows(session_factory):
    from sqlalchemy import select

    from app.db.models import Citation, ModelRun

    async with session_factory() as session:
        runs = list((await session.execute(select(ModelRun))).scalars().all())
        citations = list((await session.execute(select(Citation))).scalars().all())
        return runs, citations
