import json
import socket
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ingestion.fetcher import SourceFetcher, UnsafeUrlError
from app.obsidian.markdown import parse_note
from conftest import wait_for_job
from fixture_sources import build_docx_fixture, build_html_fixture, build_pdf_fixture


def _chunk_locators(client: TestClient, item_id: str) -> list[dict[str, object]]:
    database_path = client.app.state.settings.database_path
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source_locator FROM chunks WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _submit_file(
    client: TestClient, filename: str, content: bytes, media_type: str
) -> tuple[str, dict[str, object]]:
    response = client.post(
        "/api/sources/files",
        files={"file": (filename, content, media_type)},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    job = wait_for_job(client, payload["job_id"])
    assert job["kind"] == "ingest_source"
    assert job["state"] == "succeeded"
    return payload["item_id"], payload


def test_pdf_source_pipeline_keeps_page_locators_and_publishes(
    client: TestClient, settings
) -> None:
    item_id, submission = _submit_file(
        client, "synthetic-guide.pdf", build_pdf_fixture(), "application/pdf"
    )
    assert submission["deduplicated"] is False
    item = client.get(f"/api/items/{item_id}").json()
    assert item["source_type"] == "pdf"
    assert item["status"] == "pending_review"
    assert item["source_metadata"]["page_count"] == 2
    assert {
        segment["locator"]["page"] for segment in item["source_metadata"]["segments"]
    } == {1, 2}
    assert "第二页内容" in item["body"]

    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    locators = _chunk_locators(client, item_id)
    assert {locator["page"] for locator in locators} == {1, 2}
    assert all(locator["kind"] == "pdf" for locator in locators)

    note_path = settings.managed_vault_root / published.json()["note_relative_path"]
    note = parse_note(note_path.read_text(encoding="utf-8"))
    assert note.metadata["source_type"] == "pdf"


def test_docx_source_pipeline_keeps_heading_locators_and_deduplicates(
    client: TestClient,
) -> None:
    content = build_docx_fixture()
    item_id, _ = _submit_file(
        client,
        "synthetic-guide.docx",
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    repeated = client.post(
        "/api/sources/files",
        files={
            "file": (
                "renamed-copy.docx",
                content,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["deduplicated"] is True
    assert repeated.json()["item_id"] == item_id

    item = client.get(f"/api/items/{item_id}").json()
    assert item["source_metadata"]["title"] == "合成 DOCX 来源"
    assert any(
        segment["locator"]["heading_path"] == ["DOCX 知识指南", "审核流程"]
        for segment in item["source_metadata"]["segments"]
    )
    assert "合成 fixture" in item["body"]
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    locators = _chunk_locators(client, item_id)
    assert any(locator["heading_path"] == ["DOCX 知识指南"] for locator in locators)
    assert any(locator["heading_path"] == ["DOCX 知识指南", "审核流程"] for locator in locators)
    assert any(locator["element"] == "table_row" for locator in locators)


def test_webpage_source_pipeline_fetches_snapshot_and_keeps_url_locators(
    client: TestClient, settings
) -> None:
    html = build_html_fixture()

    def resolve_public(_host: str, port: int, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/guide"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html,
            request=request,
        )

    source_fetcher = client.app.state.stage2_service.source_fetcher
    source_fetcher.resolve_host = resolve_public
    source_fetcher.transport = httpx.MockTransport(handler)
    response = client.post(
        "/api/sources/url",
        json={"url": "https://example.test/guide"},
    )
    assert response.status_code == 202, response.text
    item_id = response.json()["item_id"]
    wait_for_job(client, response.json()["job_id"])
    item = client.get(f"/api/items/{item_id}").json()
    assert item["source_type"] == "webpage"
    assert item["source_metadata"]["url"] == "https://example.test/guide"
    assert item["source_metadata"]["title"] == "静态网页合成指南"
    assert "导航不应进入正文" not in item["body"]
    assert "网页正文第一段" in item["body"]

    with sqlite3.connect(settings.database_path) as connection:
        artifacts = connection.execute(
            "SELECT artifact_type, media_type FROM source_artifacts "
            "WHERE knowledge_item_id = ? ORDER BY created_at",
            (item_id,),
        ).fetchall()
    assert artifacts == [
        ("original_input", "text/uri-list"),
        ("web_snapshot", "text/html"),
    ]
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    locators = _chunk_locators(client, item_id)
    assert locators
    assert all(locator["url"] == "https://example.test/guide" for locator in locators)
    assert any(locator["heading_path"] == ["静态网页指南"] for locator in locators)
    note_path = settings.managed_vault_root / published.json()["note_relative_path"]
    note = parse_note(note_path.read_text(encoding="utf-8"))
    assert note.metadata["source_url"] == "https://example.test/guide"


def test_url_endpoint_blocks_private_address(client: TestClient) -> None:
    response = client.post(
        "/api/sources/url",
        json={"url": "http://127.0.0.1:8000/private"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_url"


@pytest.mark.asyncio
async def test_url_fetcher_revalidates_redirect_destination() -> None:
    def resolve_public(_host: str, port: int, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    fetcher = SourceFetcher(
        resolve_host=resolve_public,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch("https://example.test/redirect")
