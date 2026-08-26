import time

from fastapi.testclient import TestClient

from app.obsidian.markdown import parse_note
from conftest import wait_for_job


def submit_and_wait(
    client: TestClient,
    content: str = "# 本地知识\n\n这是阶段 2 的测试正文。",
    source_type: str = "markdown",
) -> tuple[str, str]:
    response = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": source_type},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    wait_for_job(client, payload["job_id"])
    return payload["item_id"], payload["job_id"]


def test_text_markdown_ingestion_dedup_and_draft(client: TestClient) -> None:
    item_id, _ = submit_and_wait(client)
    item = client.get(f"/api/items/{item_id}").json()
    assert item["status"] == "pending_review"
    assert item["summary"] == "确定性测试摘要"
    assert item["suggested_tags"] == ["测试", "阶段2"]
    duplicate = client.post(
        "/api/sources/text",
        json={"content": "# 本地知识\r\n\r\n这是阶段 2 的测试正文。", "source_type": "markdown"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["item_id"] == item_id


def test_idempotency_key_reuse_rejects_different_content(client: TestClient) -> None:
    first = client.post(
        "/api/sources/text",
        json={"content": "第一份内容", "idempotency_key": "stable-key"},
    )
    assert first.status_code == 202
    wait_for_job(client, first.json()["job_id"])
    repeated = client.post(
        "/api/sources/text",
        json={"content": "第一份内容", "idempotency_key": "stable-key"},
    )
    assert repeated.status_code == 202
    assert repeated.json()["deduplicated"] is True
    conflict = client.post(
        "/api/sources/text",
        json={"content": "不同内容", "idempotency_key": "stable-key"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_review_publish_frontmatter_chunk_qdrant_and_soft_delete(
    client: TestClient, settings
) -> None:
    item_id, _ = submit_and_wait(client, "纯文本输入\n\n第二段证据。", "text")
    reviewed = client.post(f"/api/items/{item_id}/review", json={"approved": True})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "reviewed"
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    payload = published.json()
    assert payload["status"] == "published"
    relative_path = payload["note_relative_path"]
    note_path = settings.managed_vault_root / relative_path
    assert note_path.is_file()
    parsed = parse_note(note_path.read_text(encoding="utf-8"))
    assert parsed.zhiliu_id == item_id
    assert parsed.metadata["source_type"] == "text"
    assert "纯文本输入" in parsed.body
    opened = client.post(f"/api/obsidian/open/{item_id}")
    assert opened.status_code == 200
    assert opened.json()["uri"].startswith("obsidian://open?")
    assert settings.qdrant_path.joinpath("collection", "knowledge_chunks").exists()
    deleted = client.delete(f"/api/items/{item_id}")
    assert deleted.status_code == 204
    assert note_path.exists()
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_external_note_change_rescan_reindexes_latest(
    client: TestClient, settings
) -> None:
    item_id, _ = submit_and_wait(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    first = client.post(f"/api/items/{item_id}/publish").json()
    first_version = first["version_no"]
    note_path = settings.managed_vault_root / first["note_relative_path"]
    raw = note_path.read_text(encoding="utf-8")
    note_path.write_text(
        raw.replace("这是阶段 2 的测试正文。", "这是 Obsidian 外部修改后的最新正文。"),
        encoding="utf-8",
    )
    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    assert rescanned.json()["changed"] == 1
    latest = client.get(f"/api/items/{item_id}").json()
    assert latest["version_no"] == first_version + 1
    assert "外部修改后的最新正文" in latest["body"]
    assert latest["sync_state"] == "synced"


def test_watcher_detects_file_modification(client: TestClient, settings) -> None:
    item_id, _ = submit_and_wait(client, "监听前正文", "text")
    client.post(f"/api/items/{item_id}/review", json={})
    published = client.post(f"/api/items/{item_id}/publish").json()
    original_version = published["version_no"]
    note_path = settings.managed_vault_root / published["note_relative_path"]
    note_path.write_text(
        note_path.read_text(encoding="utf-8").replace("监听前正文", "监听后正文"),
        encoding="utf-8",
    )
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        latest = client.get(f"/api/items/{item_id}").json()
        if latest["version_no"] > original_version:
            break
        time.sleep(0.05)
    assert latest["version_no"] == original_version + 1
    assert "监听后正文" in latest["body"]


def test_published_edit_requires_current_hash(client: TestClient) -> None:
    item_id, _ = submit_and_wait(client, "冲突保护正文", "text")
    client.post(f"/api/items/{item_id}/review", json={})
    published = client.post(f"/api/items/{item_id}/publish").json()
    conflict = client.patch(
        f"/api/items/{item_id}",
        json={"body": "不能覆盖", "expected_content_hash": "0" * 64},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "content_conflict"
    updated = client.patch(
        f"/api/items/{item_id}",
        json={
            "body": "安全网页编辑",
            "expected_content_hash": published["content_hash"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert "安全网页编辑" in updated.json()["body"]
