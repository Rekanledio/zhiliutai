from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from conftest import wait_for_job


def _submit_text(client: TestClient, content: str) -> tuple[str, str]:
    response = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    return payload["item_id"], payload["job_id"]


def test_dashboard_reports_today_pending_and_recent_items(client: TestClient) -> None:
    item_id, job_id = _submit_text(client, "Dashboard 的真实正文")
    wait_for_job(client, job_id)

    pending_dashboard = client.get("/api/dashboard")
    assert pending_dashboard.status_code == 200, pending_dashboard.text
    pending_payload = pending_dashboard.json()
    assert pending_payload["stats"]["today_added"] >= 1
    assert pending_payload["stats"]["pending_review"] == 1
    assert pending_payload["pending_reviews"][0]["id"] == item_id
    assert pending_payload["pending_reviews"][0]["status"] == "pending_review"
    assert pending_payload["recent_items"] == []

    reviewed = client.post(f"/api/items/{item_id}/review", json={})
    assert reviewed.status_code == 200, reviewed.text
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text

    recent_dashboard = client.get("/api/dashboard")
    assert recent_dashboard.status_code == 200, recent_dashboard.text
    recent_payload = recent_dashboard.json()
    assert recent_payload["stats"]["knowledge_count"] == 1
    assert recent_payload["stats"]["pending_review"] == 0
    assert recent_payload["recent_items"][0]["id"] == item_id
    assert recent_payload["recent_items"][0]["status"] == "published"


def test_job_response_exposes_lifecycle_attempts_and_safe_summaries(
    client: TestClient,
) -> None:
    item_id, job_id = _submit_text(client, "Job 生命周期正文")
    wait_for_job(client, job_id)

    initial = client.get(f"/api/jobs/{job_id}")
    assert initial.status_code == 200, initial.text
    initial_payload = initial.json()
    assert initial_payload["started_at"]
    assert initial_payload["finished_at"]
    assert isinstance(initial_payload["duration_ms"], int)
    assert initial_payload["duration_ms"] >= 0
    assert len(initial_payload["attempts"]) == 1
    attempt = initial_payload["attempts"][0]
    assert attempt["attempt_no"] == 1
    assert attempt["state"] == "succeeded"
    assert attempt["started_at"]
    assert attempt["finished_at"]
    assert isinstance(attempt["duration_ms"], int)

    with sqlite3.connect(client.app.state.settings.database_path) as connection:
        connection.execute(
            "UPDATE processing_jobs SET payload_json = ?, result_json = ?, error_json = ? WHERE id = ?",
            (
                json.dumps({"url": "https://example.test/?token=SECRET", "path": "D:\\Vault\\secret"}),
                json.dumps(
                    {
                        "item_id": item_id,
                        "url": "https://example.test/?token=SECRET",
                        "response": "UPSTREAM_RESPONSE_SECRET",
                    }
                ),
                json.dumps(
                    {
                        "code": "job_failed",
                        "type": "RuntimeError",
                        "message": "https://example.test/path?token=SECRET D:\\Vault\\secret",
                    }
                ),
                job_id,
            ),
        )
        connection.commit()

    safe_payload = client.get(f"/api/jobs/{job_id}").json()
    assert safe_payload["result"] == {"item_id": item_id}
    assert "SECRET" not in json.dumps(safe_payload, ensure_ascii=False)
    assert "UPSTREAM_RESPONSE_SECRET" not in json.dumps(safe_payload, ensure_ascii=False)
    assert "payload_json" not in safe_payload
    assert safe_payload["error"]["message"] == "处理失败"
