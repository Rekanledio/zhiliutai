import socket

import httpx
from fastapi.testclient import TestClient

from conftest import wait_for_job
from fixture_sources import build_pdf_fixture


def _publish_text(client: TestClient, content: str, source_type: str = "markdown") -> str:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": source_type},
    )
    assert submitted.status_code == 202, submitted.text
    item_id = submitted.json()["item_id"]
    wait_for_job(client, submitted.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{item_id}/publish").status_code == 200
    return item_id


def test_search_api_returns_current_results_and_structured_obsidian_citation(
    client: TestClient,
) -> None:
    item_id = _publish_text(client, "混合检索的 SQLite 证据必须可以追溯。")

    response = client.post("/api/search", json={"query": "SQLite 证据"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["evidence"]["status"] == "sufficient"
    result = next(result for result in payload["results"] if result["knowledge_item_id"] == item_id)
    citation = result["citation"]
    assert citation["content_version_id"] == result["content_version_id"]
    assert citation["locator_status"] == "fallback"
    assert citation["locator"]["kind"] == "obsidian"
    assert citation["locator"]["path"].endswith(".md")
    assert citation["target"] == {"kind": "obsidian", "item_id": item_id}
    assert "D:\\Work" not in response.text
    assert "qdrant" not in response.text.lower()


def test_search_empty_and_validation_keep_search_available_without_chat(client: TestClient) -> None:
    empty = client.post("/api/search", json={"query": "不存在的检索词"})
    assert empty.status_code == 200
    assert empty.json()["results"] == []
    assert empty.json()["evidence"]["status"] == "none"

    invalid = client.post("/api/search", json={"query": "x" * 2_001})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"


def test_pdf_search_uses_exact_page_and_controlled_artifact_target(
    client: TestClient,
) -> None:
    uploaded = client.post(
        "/api/sources/files",
        files={"file": ("guide.pdf", build_pdf_fixture(), "application/pdf")},
    )
    assert uploaded.status_code == 202, uploaded.text
    item_id = uploaded.json()["item_id"]
    wait_for_job(client, uploaded.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text

    response = client.post("/api/search", json={"query": "第二页内容"})

    assert response.status_code == 200, response.text
    result = next(
        result for result in response.json()["results"] if result["knowledge_item_id"] == item_id
    )
    citation = result["citation"]
    assert citation["locator_status"] == "exact"
    assert citation["locator"] == {"kind": "pdf", "page": 2, "page_label": "2"}
    artifact_id = citation["target"]["artifact_id"]
    artifact_response = client.get(f"/api/artifacts/{artifact_id}")
    assert artifact_response.status_code == 200
    assert artifact_response.headers["content-type"].startswith("application/pdf")

    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/artifacts/{artifact_id}").status_code == 404


def test_webpage_search_uses_saved_final_url_without_network(
    client: TestClient,
) -> None:
    def resolve_public(_host: str, port: int, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                "<html><head><title>固定网页</title></head><body>"
                "<h1>来源定位</h1><p>网页证据只来自快照。</p></body></html>"
            ).encode(),
            request=request,
        )

    source_fetcher = client.app.state.stage2_service.source_fetcher
    source_fetcher.resolve_host = resolve_public
    source_fetcher.transport = httpx.MockTransport(handler)
    submitted = client.post("/api/sources/url", json={"url": "https://example.test/fixed"})
    assert submitted.status_code == 202, submitted.text
    item_id = submitted.json()["item_id"]
    wait_for_job(client, submitted.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{item_id}/publish").status_code == 200

    response = client.post("/api/search", json={"query": "网页证据"})

    assert response.status_code == 200, response.text
    result = next(
        result for result in response.json()["results"] if result["knowledge_item_id"] == item_id
    )
    citation = result["citation"]
    assert citation["locator_status"] == "exact"
    assert citation["locator"]["url"] == "https://example.test/fixed"
    assert citation["target"] == {"kind": "url", "url": "https://example.test/fixed"}
