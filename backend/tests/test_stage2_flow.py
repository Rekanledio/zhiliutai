import sqlite3
import time

from fastapi.testclient import TestClient

from app.obsidian.markdown import parse_note
from app.services.vector_store import QdrantLocalStore
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


def _remove_frontmatter_key(note_path, key: str) -> None:
    lines = note_path.read_text(encoding="utf-8").splitlines(keepends=True)
    target = f"{key}:"
    rewritten: list[str] = []
    index = 0
    found = False
    while index < len(lines):
        line = lines[index]
        if not line.startswith((" ", "\t")) and line.rstrip("\r\n") == target:
            found = True
            index += 1
            while index < len(lines):
                following = lines[index]
                if following.strip() == "" or following.startswith((" ", "\t")):
                    index += 1
                    continue
                break
            continue
        rewritten.append(line)
        index += 1
    if not found:
        raise AssertionError(f"Frontmatter key not found: {key}")
    note_path.write_text("".join(rewritten), encoding="utf-8")


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


def test_external_note_change_rescan_reindexes_latest(client: TestClient, settings) -> None:
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
    collection = client.post(
        "/api/collections", json={"name": "监听保留合集", "description": None}
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    added = client.post(f"/api/collections/{collection_id}/items/{item_id}")
    assert added.status_code == 200, added.text
    _remove_frontmatter_key(note_path, "collections")
    original = note_path.read_text(encoding="utf-8")
    with_listener_tag = original.replace(
        '  - "测试"\n  - "阶段2"', '  - "监听标签"'
    )
    note_path.write_text(
        with_listener_tag.replace("监听前正文", "监听中正文"), encoding="utf-8"
    )
    time.sleep(0.06)
    note_path.write_text(
        with_listener_tag.replace("监听前正文", "监听后正文"), encoding="utf-8"
    )
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        latest = client.get(f"/api/items/{item_id}").json()
        if latest["version_no"] > original_version:
            break
        time.sleep(0.05)
    assert latest["version_no"] == original_version + 1
    assert "监听后正文" in latest["body"]
    time.sleep(0.4)
    stable = client.get(f"/api/items/{item_id}").json()
    assert stable["version_no"] == original_version + 1
    assert stable["confirmed_tags"] == ["监听标签"]
    assert stable["collections"] == ["监听保留合集"]

    with sqlite3.connect(settings.database_path) as connection:
        current_version_id = connection.execute(
            "SELECT current_content_version_id FROM knowledge_items WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT count(*) FROM content_versions WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
        chunk_versions = connection.execute(
            "SELECT count(*), count(DISTINCT content_version_id) "
            "FROM chunks WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()
        fts_versions = connection.execute(
            "SELECT count(*), count(DISTINCT content_version_id) "
            "FROM chunk_fts WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()
        indexed_version_id = connection.execute(
            "SELECT content_version_id FROM chunks WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]

    assert version_count == 3
    assert chunk_versions == (1, 1)
    assert fts_versions == (1, 1)
    assert indexed_version_id == current_version_id
    vector_results = QdrantLocalStore(settings.qdrant_path, 8).search([1.0] * 8, limit=100)
    assert {result["payload"]["content_version_id"] for result in vector_results} == {
        current_version_id
    }


def test_transient_invalid_markdown_is_not_reported_missing(client: TestClient, settings) -> None:
    item_id, _ = submit_and_wait(client, "瞬态文件测试", "text")
    client.post(f"/api/items/{item_id}/review", json={})
    published = client.post(f"/api/items/{item_id}/publish").json()
    note_path = settings.managed_vault_root / published["note_relative_path"]
    original = note_path.read_text(encoding="utf-8")
    note_path.write_text("---\nzhiliu_id:", encoding="utf-8")

    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200
    assert rescanned.json()["invalid"] == 1
    assert rescanned.json()["missing"] == 0
    latest_response = client.get(f"/api/items/{item_id}")
    assert latest_response.status_code == 200
    latest = latest_response.json()
    assert latest["sync_state"] == "error"
    assert latest["body"].strip() == "瞬态文件测试"

    note_path.write_text(original, encoding="utf-8")
    assert client.post("/api/obsidian/rescan").status_code == 200


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
    with sqlite3.connect(client.app.state.settings.database_path) as connection:
        current_version_id = connection.execute(
            "SELECT current_content_version_id FROM knowledge_items WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
    vector_results = QdrantLocalStore(client.app.state.settings.qdrant_path, 8).search(
        [1.0] * 8, limit=100
    )
    assert {result["payload"]["content_version_id"] for result in vector_results} == {
        current_version_id
    }
