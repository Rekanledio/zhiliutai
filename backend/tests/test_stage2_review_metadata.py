from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.providers.models import DraftResult
from app.obsidian.markdown import parse_note, render_note
from conftest import wait_for_job


class SuggestedDraftProvider:
    async def create_draft(self, title: str, content: str) -> DraftResult:
        return DraftResult(
            title=title,
            body=content,
            summary="AI 摘要候选",
            suggested_tags=["人工标签"],
            suggested_collections=["AI 推荐合集"],
            prompt_version="review-metadata-v1",
        )


def _submit(
    client: TestClient, content: str = "原始审核正文"
) -> tuple[str, str]:
    response = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    wait_for_job(client, payload["job_id"])
    return payload["item_id"], payload["job_id"]


def _rewrite_frontmatter(
    note_path, *, tags: list[str], collections: list[str], body: str | None = None
) -> None:
    note = parse_note(note_path.read_text(encoding="utf-8"))
    metadata = note.metadata
    source_url = metadata.get("source_url")
    note_path.write_text(
        render_note(
            zhiliu_id=str(metadata["zhiliu_id"]),
            source_type=str(metadata["source_type"]),
            title=str(metadata["title"]),
            body=note.body if body is None else body,
            status=str(metadata["status"]),
            created_at=datetime.fromisoformat(str(metadata["created_at"])),
            updated_at=datetime.fromisoformat(str(metadata["updated_at"])),
            tags=tags,
            collections=collections,
            source_url=source_url if isinstance(source_url, str) else None,
        ),
        encoding="utf-8",
    )


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


def test_review_edit_confirmed_metadata_and_resume_are_idempotent(
    client: TestClient, settings
) -> None:
    client.app.state.stage2_service.draft_provider = SuggestedDraftProvider()
    item_id, job_id = _submit(client)

    draft = client.get(f"/api/items/{item_id}")
    assert draft.status_code == 200
    assert draft.json()["suggested_tags"] == ["人工标签"]
    assert draft.json()["suggested_collections"] == ["AI 推荐合集"]
    assert draft.json()["confirmed_tags"] == []
    assert draft.json()["collections"] == []

    edited = client.post(
        f"/api/items/{item_id}/review",
        json={
            "decision": "approve",
            "title": "确认后的标题",
            "body": "确认后的正文",
            "summary": "确认后的摘要",
            "suggested_tags": ["确认标签", "确认标签"],
            "suggested_collections": ["确认合集", "确认合集"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "reviewed"
    assert edited.json()["title"] == "确认后的标题"
    assert edited.json()["body"] == "确认后的正文\n"
    assert edited.json()["summary"] == "确认后的摘要"
    assert edited.json()["suggested_tags"] == ["确认标签"]
    assert edited.json()["suggested_collections"] == ["确认合集"]
    assert client.get(f"/api/jobs/{job_id}").json()["stage"] == "pending_publish"

    repeated_review = client.post(
        f"/api/items/{item_id}/review", json={"decision": "approve"}
    )
    assert repeated_review.status_code == 200, repeated_review.text
    assert repeated_review.json()["status"] == "reviewed"

    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    payload = published.json()
    assert payload["status"] == "published"
    assert payload["confirmed_tags"] == ["确认标签"]
    assert payload["collections"] == ["确认合集"]
    note = parse_note((settings.managed_vault_root / payload["note_relative_path"]).read_text(encoding="utf-8"))
    assert note.metadata["tags"] == ["确认标签"]
    assert note.metadata["collections"] == ["确认合集"]

    with sqlite3.connect(settings.database_path) as connection:
        tag_rows = connection.execute(
            "SELECT count(*) FROM knowledge_item_tags WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
        collection_rows = connection.execute(
            "SELECT count(*) FROM collection_items WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
    assert tag_rows == 1
    assert collection_rows == 1

    version_id = payload["current_content_version_id"]
    repeated_publish = client.post(f"/api/items/{item_id}/publish")
    assert repeated_publish.status_code == 200, repeated_publish.text
    assert repeated_publish.json()["current_content_version_id"] == version_id
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM content_versions WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0] == 2


def test_rescan_converges_manual_frontmatter_tags_to_confirmed_relations(
    client: TestClient, settings
) -> None:
    item_id, _job_id = _submit(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish").json()
    note_path = settings.managed_vault_root / published["note_relative_path"]
    raw = note_path.read_text(encoding="utf-8")
    edited = raw.replace(
        '  - "测试"\n  - "阶段2"',
        '  - "手工确认标签"',
    )
    if edited == raw:
        edited = raw.replace("tags:\n", 'tags:\n  - "手工确认标签"\n', 1)
    note_path.write_text(edited, encoding="utf-8")

    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    current = client.get(f"/api/items/{item_id}").json()
    assert current["confirmed_tags"] == ["手工确认标签"]


@pytest.mark.parametrize(
    ("tags", "collections"),
    [
        (["仅标签"], []),
        ([], ["仅合集"]),
        (["同时标签"], ["同时合集"]),
        ([], []),
    ],
    ids=["tags-only", "collections-only", "both", "both-empty"],
)
def test_rescan_converges_independent_frontmatter_relations(
    client: TestClient, settings, tags: list[str], collections: list[str]
) -> None:
    item_id, _job_id = _submit(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    payload = published.json()
    note_path = settings.managed_vault_root / payload["note_relative_path"]

    _rewrite_frontmatter(
        note_path,
        tags=tags,
        collections=collections,
    )
    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text

    current = client.get(f"/api/items/{item_id}")
    assert current.status_code == 200, current.text
    assert current.json()["confirmed_tags"] == tags
    assert current.json()["collections"] == collections


def test_rescan_preserves_missing_collections_until_explicit_empty(
    client: TestClient, settings
) -> None:
    item_id, _job_id = _submit(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    payload = published.json()
    note_path = settings.managed_vault_root / payload["note_relative_path"]

    collection = client.post(
        "/api/collections", json={"name": "缺失字段合集", "description": None}
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    added = client.post(f"/api/collections/{collection_id}/items/{item_id}")
    assert added.status_code == 200, added.text

    _remove_frontmatter_key(note_path, "collections")
    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    preserved = client.get(f"/api/items/{item_id}")
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["collections"] == ["缺失字段合集"]

    _rewrite_frontmatter(
        note_path,
        tags=preserved.json()["confirmed_tags"],
        collections=[],
    )
    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    cleared = client.get(f"/api/items/{item_id}")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["collections"] == []


@pytest.mark.parametrize(
    ("tags", "collections"),
    [
        ([r"C:\Users\not-allowed\tag"], []),
        ([], ["/not/allowed/collection"]),
    ],
    ids=["invalid-tag", "invalid-collection"],
)
def test_invalid_frontmatter_relations_keep_published_current_content(
    client: TestClient, settings, tags: list[str], collections: list[str]
) -> None:
    item_id, _job_id = _submit(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    before = published.json()
    note_path = settings.managed_vault_root / before["note_relative_path"]
    _rewrite_frontmatter(
        note_path,
        tags=tags,
        collections=collections,
        body="非法 Frontmatter 下不应成为 current 的正文",
    )

    rescanned = client.post("/api/obsidian/rescan")
    assert rescanned.status_code == 200, rescanned.text
    assert rescanned.json()["invalid"] >= 1

    after = client.get(f"/api/items/{item_id}")
    assert after.status_code == 200, after.text
    after_payload = after.json()
    assert after_payload["status"] == "published"
    assert after_payload["current_content_version_id"] == before["current_content_version_id"]
    assert after_payload["body"] == before["body"]
    assert after_payload["confirmed_tags"] == before["confirmed_tags"]
    assert after_payload["collections"] == before["collections"]

    with sqlite3.connect(settings.database_path) as connection:
        version_count = connection.execute(
            "SELECT count(*) FROM content_versions WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
        binding_state = connection.execute(
            "SELECT sync_state FROM note_bindings WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
    assert version_count == 2
    assert binding_state == "error"


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_reprocess_decision_keeps_published_current_version(
    client: TestClient, decision: str
) -> None:
    item_id, _job_id = _submit(client)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    first = client.post(f"/api/items/{item_id}/publish")
    assert first.status_code == 200, first.text
    first_payload = first.json()
    old_current = first_payload["current_content_version_id"]
    old_body = first_payload["body"]

    queued = client.post(f"/api/items/{item_id}/reprocess")
    assert queued.status_code == 200, queued.text
    reprocess_job_id = queued.json()["job_id"]
    wait_for_job(client, reprocess_job_id)

    pending = client.get(f"/api/items/{item_id}").json()
    assert pending["status"] == "published"
    assert pending["current_content_version_id"] == old_current
    assert pending["pending_content_version_id"]

    decided = client.post(
        f"/api/items/{item_id}/review", json={"decision": decision}
    )
    assert decided.status_code == 200, decided.text
    decided_payload = decided.json()
    assert decided_payload["status"] == "published"
    assert decided_payload["current_content_version_id"] == old_current
    assert decided_payload["pending_content_version_id"] is None
    assert decided_payload["body"] == old_body
    assert client.get(f"/api/jobs/{reprocess_job_id}").json()["stage"] == (
        "rejected" if decision == "reject" else "cancelled"
    )

    repeated = client.post(
        f"/api/items/{item_id}/review", json={"decision": decision}
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["current_content_version_id"] == old_current


def test_item_source_metadata_is_bounded_and_redacted(client: TestClient) -> None:
    item_id, _job_id = _submit(client)
    version_id = client.get(f"/api/items/{item_id}").json()[
        "current_content_version_id"
    ]
    secret = "SOURCE_METADATA_SECRET"
    absolute_path = r"C:\Users\Lenovo\Vault Root\secret.md"
    metadata = {
        "source_type": "webpage",
        "url": f"https://example.test/guide?session={secret}",
        "title": "合成来源",
        "segments": [
            {
                "text": f"不应重复返回的正文 {secret}",
                "locator": {
                    "url": f"https://example.test/guide?token={secret}",
                    "path": absolute_path,
                    "page": 3,
                },
            }
        ],
        "manifest": {"authorization": f"Bearer {secret}"},
    }
    with sqlite3.connect(client.app.state.settings.database_path) as connection:
        connection.execute(
            "UPDATE content_versions SET source_metadata_json = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), version_id),
        )
        connection.execute(
            "UPDATE content_versions SET suggested_tags_json = ?, suggested_collections_json = ? WHERE id = ?",
            (json.dumps([absolute_path, secret], ensure_ascii=False), json.dumps([absolute_path, secret], ensure_ascii=False), version_id),
        )
        connection.commit()

    response = client.get(f"/api/items/{item_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    public_metadata = payload["source_metadata"]
    assert public_metadata["url"] == "https://example.test/guide"
    assert public_metadata["segments"] == [
        {"locator": {"url": "https://example.test/guide", "path": "[REDACTED]", "page": 3}}
    ]
    assert payload["suggested_tags"] == []
    assert payload["suggested_collections"] == []
    assert "manifest" not in public_metadata
    assert secret not in response.text
    assert absolute_path not in response.text


def test_items_filters_return_current_published_organization_metadata(
    client: TestClient,
) -> None:
    first_id, _ = _submit(client)
    assert client.post(f"/api/items/{first_id}/review", json={}).status_code == 200
    first_published = client.post(f"/api/items/{first_id}/publish")
    assert first_published.status_code == 200, first_published.text

    second_id, _ = _submit(client, "第二条审核正文")
    assert client.post(f"/api/items/{second_id}/review", json={}).status_code == 200
    second_published = client.post(f"/api/items/{second_id}/publish")
    assert second_published.status_code == 200, second_published.text

    collection = client.post(
        "/api/collections", json={"name": "筛选合集", "description": None}
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    added = client.post(f"/api/collections/{collection_id}/items/{second_id}")
    assert added.status_code == 200, added.text

    filtered = client.get(
        "/api/items",
        params={
            "status": "published",
            "source_type": "markdown",
            "tag": "测试",
            "collection": "筛选合集",
            "created_after": "2000-01-01",
            "created_before": "2100-01-01",
        },
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()] == [second_id]
    assert filtered.json()[0]["confirmed_tags"] == ["测试", "阶段2"]
    assert filtered.json()[0]["collections"] == ["筛选合集"]
    assert filtered.json()[0]["current_content_version_id"]

    empty = client.get("/api/items", params={"created_after": "2100-01-01"})
    assert empty.status_code == 200
    assert empty.json() == []
    invalid = client.get("/api/items", params={"status": "deleted"})
    assert invalid.status_code == 422
    invalid_date = client.get("/api/items", params={"created_after": "not-a-date"})
    assert invalid_date.status_code == 422
