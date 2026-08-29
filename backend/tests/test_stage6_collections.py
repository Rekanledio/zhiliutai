from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collection, CollectionItem
from app.obsidian.markdown import ObsidianVault, parse_note
from conftest import wait_for_job


def publish_item(
    client: TestClient,
    content: str = "合集测试正文",
    source_type: str = "markdown",
) -> tuple[str, dict[str, object]]:
    response = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": source_type},
    )
    assert response.status_code == 202, response.text
    submission = response.json()
    wait_for_job(client, submission["job_id"])
    assert client.post(f"/api/items/{submission['item_id']}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{submission['item_id']}/publish")
    assert published.status_code == 200, published.text
    return submission["item_id"], published.json()


def create_collection(
    client: TestClient, name: str = "人工合集", description: str | None = None
) -> dict[str, object]:
    response = client.post(
        "/api/collections",
        json={"name": name, "description": description},
    )
    assert response.status_code == 201, response.text
    return response.json()


def insert_collection_relation(settings, collection_id: str, item_id: str) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO collection_items "
            "(id, collection_id, knowledge_item_id, created_at) VALUES (?, ?, ?, ?)",
            (
                str(uuid4()),
                collection_id,
                item_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def test_collection_contracts_crud_and_sensitive_input_fail_closed(client: TestClient) -> None:
    created = create_collection(client, "  人工合集  ", "  用于人工整理  ")
    assert created["name"] == "人工合集"
    assert created["description"] == "用于人工整理"
    assert created["items"] == []
    assert created["related_tags"] == []
    assert created["moc_enabled"] is False
    assert created["moc_status"] == "not_enabled"

    extra = client.post(
        "/api/collections",
        json={"name": "不会保存", "unexpected": "COLLECTION_EXTRA_SENTINEL"},
    )
    assert extra.status_code == 422
    assert "COLLECTION_EXTRA_SENTINEL" not in extra.text

    unsafe_values = [
        "Cookie: COLLECTION_COOKIE_SENTINEL",
        "Authorization: Bearer COLLECTION_AUTH_SENTINEL",
        "api_key=COLLECTION_KEY_SENTINEL",
        r"C:\Users\Lenovo\Vault Root\COLLECTION_PATH_SENTINEL",
        "/tmp/collection/COLLECTION_UNIX_SENTINEL",
        "Traceback COLLECTION_TRACEBACK_SENTINEL",
    ]
    for value in unsafe_values:
        rejected = client.post(
            "/api/collections",
            json={"name": value, "description": "safe"},
        )
        assert rejected.status_code == 422
        assert all(sentinel not in rejected.text for sentinel in (
            "COLLECTION_COOKIE_SENTINEL",
            "COLLECTION_AUTH_SENTINEL",
            "COLLECTION_KEY_SENTINEL",
            "COLLECTION_PATH_SENTINEL",
            "COLLECTION_UNIX_SENTINEL",
            "COLLECTION_TRACEBACK_SENTINEL",
        ))

    bad_description = client.post(
        "/api/collections",
        json={"name": "安全名称", "description": "Cookie: DESCRIPTION_COOKIE_SENTINEL"},
    )
    assert bad_description.status_code == 422
    assert "DESCRIPTION_COOKIE_SENTINEL" not in bad_description.text

    conflict = client.post("/api/collections", json={"name": "人工合集"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "collection_name_conflict"

    updated = client.patch(
        f"/api/collections/{created['id']}",
        json={"name": "改名合集", "description": "新的说明"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名合集"
    assert updated.json()["description"] == "新的说明"
    assert client.get("/api/collections/not-a-uuid").status_code == 404
    assert client.get("/api/collections/not-a-uuid").json()["error"]["code"] == "collection_not_found"


def test_publish_renders_existing_collection_relation(
    client: TestClient, settings
) -> None:
    response = client.post(
        "/api/sources/text",
        json={"content": "发布前已存在合集关系", "source_type": "text"},
    )
    submission = response.json()
    wait_for_job(client, submission["job_id"])
    collection = create_collection(client, "发布时合集")
    insert_collection_relation(settings, collection["id"], submission["item_id"])

    assert client.post(f"/api/items/{submission['item_id']}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{submission['item_id']}/publish")
    assert published.status_code == 200, published.text
    note_path = settings.managed_vault_root / published.json()["note_relative_path"]
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == ["发布时合集"]


def test_collection_membership_is_idempotent_and_only_current_published_counts(
    client: TestClient, settings
) -> None:
    item_id, published = publish_item(client, "成员正文")
    collection = create_collection(client, "有效成员", "说明")
    collection_id = collection["id"]
    note_path = settings.managed_vault_root / published["note_relative_path"]
    before = parse_note(note_path.read_text(encoding="utf-8"))
    before_item = client.get(f"/api/items/{item_id}").json()

    added = client.post(f"/api/collections/{collection_id}/items/{item_id}")
    assert added.status_code == 200, added.text
    assert [item["id"] for item in added.json()["items"]] == [item_id]
    assert added.json()["item_count"] == 1
    parsed = parse_note(note_path.read_text(encoding="utf-8"))
    assert parsed.metadata["collections"] == ["有效成员"]
    assert parsed.body == before.body

    repeated = client.post(f"/api/collections/{collection_id}/items/{item_id}")
    assert repeated.status_code == 200
    with sqlite3.connect(settings.database_path) as connection:
        relation_count = connection.execute(
            "SELECT count(*) FROM collection_items WHERE collection_id = ? AND knowledge_item_id = ?",
            (collection_id, item_id),
        ).fetchone()[0]
    assert relation_count == 1
    after_item = client.get(f"/api/items/{item_id}").json()
    assert after_item["current_content_version_id"] == before_item["current_content_version_id"]
    assert after_item["content_hash"] == before_item["content_hash"]

    removed = client.delete(f"/api/collections/{collection_id}/items/{item_id}")
    assert removed.status_code == 200
    assert removed.json()["items"] == []
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == []
    assert client.delete(f"/api/collections/{collection_id}/items/{item_id}").status_code == 200

    pending_response = client.post(
        "/api/sources/text",
        json={"content": "未发布不能入合集", "source_type": "text"},
    )
    pending_submission = pending_response.json()
    wait_for_job(client, pending_submission["job_id"])
    rejected = client.post(
        f"/api/collections/{collection_id}/items/{pending_submission['item_id']}"
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "collection_item_invalid"

    deleted_item_id, _ = publish_item(client, "软删除成员")
    assert client.post(
        f"/api/collections/{collection_id}/items/{deleted_item_id}"
    ).status_code == 200
    assert client.delete(f"/api/items/{deleted_item_id}").status_code == 204
    listed = client.get("/api/collections").json()
    summary = next(row for row in listed if row["id"] == collection_id)
    assert summary["item_count"] == 0
    assert client.get(f"/api/collections/{collection_id}").json()["items"] == []

    assert note_path.is_file()
    assert client.get(f"/api/items/{item_id}").status_code == 200

def test_collection_mutations_preserve_disk_tags_and_repair_relation_drift(
    client: TestClient, settings
) -> None:
    item_id, published = publish_item(client, "用户标签不能被覆盖")
    collection = create_collection(client, "标签保留合集")
    note_path = settings.managed_vault_root / published["note_relative_path"]
    original = note_path.read_text(encoding="utf-8")
    edited = original.replace(
        '  - "测试"\n  - "阶段2"',
        '  - "用户标签一"\n  - "用户标签二"',
    )
    assert edited != original
    note_path.write_text(edited, encoding="utf-8")
    before_item = client.get(f"/api/items/{item_id}").json()

    added = client.post(f"/api/collections/{collection['id']}/items/{item_id}")
    assert added.status_code == 200, added.text
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["tags"] == [
        "用户标签一",
        "用户标签二",
    ]
    after_add = client.get(f"/api/items/{item_id}").json()
    assert after_add["current_content_version_id"] == before_item["current_content_version_id"]
    assert after_add["content_hash"] == before_item["content_hash"]

    stable_bytes = note_path.read_bytes()
    stable_mtime = note_path.stat().st_mtime_ns
    repeated = client.post(f"/api/collections/{collection['id']}/items/{item_id}")
    assert repeated.status_code == 200
    assert note_path.read_bytes() == stable_bytes
    assert note_path.stat().st_mtime_ns == stable_mtime
    assert repeated.json()["item_count"] == 1

    renamed = client.patch(
        f"/api/collections/{collection['id']}",
        json={"name": "标签保留改名"},
    )
    assert renamed.status_code == 200, renamed.text
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["tags"] == [
        "用户标签一",
        "用户标签二",
    ]
    removed = client.delete(
        f"/api/collections/{collection['id']}/items/{item_id}"
    )
    assert removed.status_code == 200, removed.text
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["tags"] == [
        "用户标签一",
        "用户标签二",
    ]
    final_item = client.get(f"/api/items/{item_id}").json()
    assert final_item["current_content_version_id"] == before_item["current_content_version_id"]
    assert final_item["content_hash"] == before_item["content_hash"]

    drifted = note_path.read_text(encoding="utf-8").replace(
        "collections:\n",
        'collections:\n  - "标签保留改名"\n',
        1,
    )
    note_path.write_text(drifted, encoding="utf-8")
    healed = client.post(f"/api/collections/{collection['id']}/items/{item_id}")
    assert healed.status_code == 200, healed.text
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == [
        "标签保留改名"
    ]


def test_collection_rescan_converges_safe_frontmatter_and_rejects_invalid_values(
    client: TestClient, settings
) -> None:
    item_id, published = publish_item(client, "Frontmatter 收敛正文")
    first = create_collection(client, "第一合集")
    second = create_collection(client, "第二合集")
    assert client.post(f"/api/collections/{first['id']}/items/{item_id}").status_code == 200
    note_path = settings.managed_vault_root / published["note_relative_path"]
    original = note_path.read_text(encoding="utf-8")
    edited = original.replace(
        'collections:\n  - "第一合集"',
        'collections:\n  - "第二合集"\n  - "第二合集"',
    )
    note_path.write_text(edited, encoding="utf-8")
    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200
    assert client.get(f"/api/collections/{first['id']}").json()["items"] == []
    assert [item["id"] for item in client.get(f"/api/collections/{second['id']}").json()["items"]] == [item_id]

    invalid = edited.replace(
        'collections:\n  - "第二合集"\n  - "第二合集"',
        'collections:\n  - "C:\\Users\\Lenovo\\COLLECTION_FRONTMATTER_SENTINEL"',
    )
    note_path.write_text(invalid, encoding="utf-8")
    invalid_scan = client.post("/api/obsidian/rescan")
    assert invalid_scan.status_code == 200
    assert invalid_scan.json()["invalid"] == 1
    assert "COLLECTION_FRONTMATTER_SENTINEL" not in invalid_scan.text
    assert [item["id"] for item in client.get(f"/api/collections/{second['id']}").json()["items"]] == [item_id]


def test_rescan_creates_safe_manual_collection_and_rejects_dangerous_name(
    client: TestClient, settings
) -> None:
    item_id, published = publish_item(client, "手工合集发现")
    note_path = settings.managed_vault_root / published["note_relative_path"]
    original = note_path.read_text(encoding="utf-8")
    edited = original.replace(
        "collections:\n",
        'collections:\n  - "手工发现合集"\n',
        1,
    )
    note_path.write_text(edited, encoding="utf-8")

    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    assert rescanned.json()["invalid"] == 0
    listed = client.get("/api/collections").json()
    created = next(row for row in listed if row["name"] == "手工发现合集")
    assert client.get(f"/api/collections/{created['id']}").json()["items"][0]["id"] == item_id

    dangerous = edited.replace(
        'collections:\n  - "手工发现合集"',
        'collections:\n  - "/tmp/MANUAL_COLLECTION_PATH_SENTINEL"',
    )
    note_path.write_text(dangerous, encoding="utf-8")
    rejected = client.post("/api/obsidian/rescan")
    assert rejected.status_code == 200
    assert rejected.json()["invalid"] == 1
    assert "MANUAL_COLLECTION_PATH_SENTINEL" not in rejected.text
    assert not any(
        row["name"] == "MANUAL_COLLECTION_PATH_SENTINEL"
        for row in client.get("/api/collections").json()
    )


def test_invalid_current_version_cannot_be_added_or_counted(
    client: TestClient, settings
) -> None:
    item_id, _published = publish_item(client, "当前版本归属校验")
    other_item_id, _other_published = publish_item(client, "另一个条目版本")
    collection = create_collection(client, "错误版本合集")
    with sqlite3.connect(settings.database_path) as connection:
        other_version_id = connection.execute(
            "SELECT current_content_version_id FROM knowledge_items WHERE id = ?",
            (other_item_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE knowledge_items SET current_content_version_id = ? WHERE id = ?",
            (other_version_id, item_id),
        )
    rejected = client.post(f"/api/collections/{collection['id']}/items/{item_id}")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "collection_item_invalid"
    insert_collection_relation(settings, collection["id"], item_id)
    summary = next(
        row
        for row in client.get("/api/collections").json()
        if row["id"] == collection["id"]
    )
    assert summary["item_count"] == 0
    assert client.get(f"/api/collections/{collection['id']}").json()["items"] == []


@pytest.mark.parametrize("failure_kind", ["stage_write", "commit", "database"])
def test_collection_mutation_compensates_staged_vault_on_failure(
    client: TestClient, settings, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    item_id, published = publish_item(client, f"补偿故障 {failure_kind}")
    collection = create_collection(client, f"补偿合集 {failure_kind}")
    note_path = settings.managed_vault_root / published["note_relative_path"]
    original_raw = note_path.read_bytes()
    failed = False

    if failure_kind == "stage_write":
        original_stage_write = ObsidianVault.stage_write

        def fail_stage_write(self, relative_path: str, content: str):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("synthetic-stage-write")
            return original_stage_write(self, relative_path, content)

        monkeypatch.setattr(ObsidianVault, "stage_write", fail_stage_write)
    elif failure_kind == "commit":
        original_commit = ObsidianVault.commit_staged

        def fail_commit(self, staged):
            nonlocal failed
            original_commit(self, staged)
            if not failed:
                failed = True
                raise OSError("synthetic-commit")

        monkeypatch.setattr(ObsidianVault, "commit_staged", fail_commit)
    else:
        original_flush = Session.flush

        def fail_flush(self, *args, **kwargs):
            nonlocal failed
            if not failed and any(isinstance(item, CollectionItem) for item in self.new):
                failed = True
                raise RuntimeError("synthetic-db-commit")
            return original_flush(self, *args, **kwargs)

        monkeypatch.setattr(Session, "flush", fail_flush)

    if failure_kind == "stage_write":
        failed_response = client.post(
            f"/api/collections/{collection['id']}/items/{item_id}"
        )
        assert failed_response.status_code == 409
        assert "synthetic-" not in failed_response.text
    else:
        expected_error = OSError if failure_kind == "commit" else RuntimeError
        with pytest.raises(expected_error):
            client.post(f"/api/collections/{collection['id']}/items/{item_id}")
    assert note_path.read_bytes() == original_raw
    assert client.get(f"/api/collections/{collection['id']}").json()["items"] == []
    assert not list(note_path.parent.glob(".*.tmp"))
    assert failed is True


def test_rename_crash_can_converge_from_markdown_on_rescan(
    client: TestClient, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id, published = publish_item(client, "合集改名崩溃收敛")
    collection = create_collection(client, "崩溃前合集")
    assert client.post(f"/api/collections/{collection['id']}/items/{item_id}").status_code == 200
    note_path = settings.managed_vault_root / published["note_relative_path"]
    service = client.app.state.knowledge_service
    monkeypatch.setattr(service, "_compensate_staged", lambda _writes: None)
    original_flush = Session.flush
    failed = False

    def fail_after_markdown_swap(self, *args, **kwargs):
        nonlocal failed
        if not failed and any(isinstance(value, Collection) for value in self.dirty):
            failed = True
            raise RuntimeError("synthetic-rename-db-rollback")
        return original_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_after_markdown_swap)
    with pytest.raises(RuntimeError):
        client.patch(
            f"/api/collections/{collection['id']}",
            json={"name": "崩溃后新合集"},
        )
    assert failed is True
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == [
        "崩溃后新合集"
    ]

    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    old_detail = client.get(f"/api/collections/{collection['id']}").json()
    assert old_detail["items"] == []
    listed = client.get("/api/collections").json()
    names = [row["name"] for row in listed]
    assert "崩溃后新合集" in names
    new_collection = next(row for row in listed if row["name"] == "崩溃后新合集")
    new_detail = client.get(f"/api/collections/{new_collection['id']}").json()
    assert [item["id"] for item in new_detail["items"]] == [item_id]


def test_collection_rename_and_delete_update_frontmatter_without_deleting_item(
    client: TestClient, settings
) -> None:
    item_id, published = publish_item(client, "删除合集不删除正文")
    collection = create_collection(client, "待改名合集")
    assert client.post(f"/api/collections/{collection['id']}/items/{item_id}").status_code == 200
    note_path = settings.managed_vault_root / published["note_relative_path"]

    renamed = client.patch(
        f"/api/collections/{collection['id']}",
        json={"name": "改名后的合集"},
    )
    assert renamed.status_code == 200, renamed.text
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == ["改名后的合集"]
    assert client.delete(f"/api/collections/{collection['id']}").status_code == 204
    assert parse_note(note_path.read_text(encoding="utf-8")).metadata["collections"] == []
    assert note_path.is_file()
    assert client.get(f"/api/items/{item_id}").status_code == 200
