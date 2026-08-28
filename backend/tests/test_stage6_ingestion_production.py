from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.providers.models import DraftResult
from conftest import wait_for_job


def _submit_text(client: TestClient, content: str) -> tuple[str, str]:
    response = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    return payload["item_id"], payload["job_id"]


def _version_count(client: TestClient, item_id: str) -> int:
    with sqlite3.connect(client.app.state.settings.database_path) as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM content_versions WHERE knowledge_item_id = ?",
                (item_id,),
            ).fetchone()[0]
        )


def test_existing_review_publish_api_resumes_one_production_graph_thread(
    client: TestClient, settings
) -> None:
    item_id, job_id = _submit_text(client, "批次 B 的生产 Graph 正文")
    waiting_review = wait_for_job(client, job_id)
    assert waiting_review["state"] == "succeeded"
    assert waiting_review["stage"] == "pending_review"
    assert settings.workflow_checkpoint_path.is_file()

    draft = client.get(f"/api/items/{item_id}").json()
    draft_version_id = draft["current_content_version_id"]
    assert draft["status"] == "pending_review"
    assert _version_count(client, item_id) == 1

    reviewed = client.post(f"/api/items/{item_id}/review", json={"approved": True})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"
    assert client.get(f"/api/jobs/{job_id}").json()["stage"] == "pending_publish"

    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    published_payload = published.json()
    assert published_payload["status"] == "published"
    assert published_payload["current_content_version_id"] != draft_version_id
    assert published_payload["pending_content_version_id"] is None
    assert _version_count(client, item_id) == 2

    duplicate = client.post(f"/api/items/{item_id}/publish")
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["current_content_version_id"] == published_payload["current_content_version_id"]
    assert _version_count(client, item_id) == 2
    final_job = client.get(f"/api/jobs/{job_id}").json()
    assert final_job["state"] == "succeeded"
    assert final_job["stage"] == "complete"


def test_production_graph_reject_and_cancel_keep_publish_out_of_the_path(client) -> None:
    rejected_item_id, rejected_job_id = _submit_text(client, "审核拒绝的生产 Graph 正文")
    wait_for_job(client, rejected_job_id)
    rejected = client.post(
        f"/api/items/{rejected_item_id}/review",
        json={"decision": "reject"},
    )
    assert rejected.status_code == 200, rejected.text
    assert client.get(f"/api/jobs/{rejected_job_id}").json()["stage"] == "rejected"
    rejected_item = client.get(f"/api/items/{rejected_item_id}").json()
    assert rejected_item["status"] == "failed"
    assert rejected_item["pending_content_version_id"] is None
    assert rejected_item["has_pending_review"] is False
    assert client.post(f"/api/items/{rejected_item_id}/publish").status_code == 409

    cancelled_item_id, cancelled_job_id = _submit_text(client, "取消的生产 Graph 正文")
    wait_for_job(client, cancelled_job_id)
    cancelled = client.post(f"/api/jobs/{cancelled_job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["stage"] == "cancelled"
    cancelled_item = client.get(f"/api/items/{cancelled_item_id}").json()
    assert cancelled_item["status"] == "failed"
    assert cancelled_item["pending_content_version_id"] is None
    assert cancelled_item["has_pending_review"] is False
    assert client.post(f"/api/items/{cancelled_item_id}/publish").status_code == 409
    assert client.post(f"/api/jobs/{cancelled_job_id}/cancel").status_code == 409


def test_production_graph_retries_failed_process_from_parent_checkpoint(client) -> None:
    class FailOnceDraft:
        def __init__(self) -> None:
            self.calls = 0

        async def create_draft(self, title: str, content: str) -> DraftResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Traceback TRACEBACK_SENTINEL API_KEY_SENTINEL")
            return DraftResult(
                title=title,
                body=content,
                summary="确定性重试摘要",
                suggested_tags=[],
                prompt_version="retry-v1",
            )

    draft_provider = FailOnceDraft()
    client.app.state.stage2_service.draft_provider = draft_provider
    item_id, job_id = _submit_text(client, "生产 Graph 可恢复正文")
    failed = wait_for_job(client, job_id, expected="failed")
    assert failed["error"]["code"] == "job_failed"
    assert failed["error"]["type"] == "RuntimeError"
    assert "TRACEBACK_SENTINEL" not in str(failed)

    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200, retried.text
    recovered = wait_for_job(client, job_id)
    assert recovered["stage"] == "pending_review"
    assert draft_provider.calls == 2
    assert _version_count(client, item_id) == 1
