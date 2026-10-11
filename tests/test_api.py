"""The HTTP surface over the service: routes, statuses, exports,
request ids, and the console."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zep_context_agent.api import create_app

AS_OF = "2026-06-01"


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def test_health_and_readiness(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    ready = client.get("/readiness")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["context_backend"] == "offline"
    assert body["cache"] == "disabled (REDIS_URL unset)"  # honest doubles
    assert body["database"] == "in-memory test doubles"


def test_request_id_propagated(client):
    resp = client.get("/health", headers={"X-Request-ID": "req-test-123"})
    assert resp.headers["X-Request-ID"] == "req-test-123"
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert first and second and first != second  # generated per request


def test_accounts_and_404(client):
    accounts = client.get("/accounts").json()
    assert len(accounts) == 40
    assert accounts[0]["account_id"] == "ACC-001"
    one = client.get("/accounts/ACC-001")
    assert one.status_code == 200
    assert one.json()["display_name"] == "Meridian Casting Co."
    assert client.get("/accounts/ACC-999").status_code == 404


def test_ingest_assemble_decide_export_flow(client):
    ingest = client.post("/accounts/ACC-001/ingest")
    assert ingest.status_code == 200
    assert "DOC-001-INJ" in ingest.json()["quarantined"]

    created = client.post("/briefs", json={"account_id": "ACC-001", "as_of": AS_OF})
    assert created.status_code == 201
    brief = created.json()
    assert brief["status"] == "AWAITING_UNDERWRITER"
    brief_id = brief["brief_id"]

    listing = client.get("/briefs", params={"account_id": "ACC-001"}).json()
    assert [b["brief_id"] for b in listing] == [brief_id]

    text = client.get(f"/briefs/{brief_id}/export", params={"format": "text"})
    assert text.status_code == 200
    assert "2,500,000" in text.text
    assert "SUPERSEDED FACTS" in text.text
    assert "5,000,000" in text.text  # shown as superseded, not hidden
    as_json = client.get(f"/briefs/{brief_id}/export").json()
    assert as_json["brief_id"] == brief_id

    decided = client.post(
        f"/briefs/{brief_id}/decision",
        json={"action": "proceed", "actor": "J. Alvarez", "reason": "Terms hold."},
    )
    assert decided.status_code == 200
    assert decided.json()["decision"]["actor"] == "J. Alvarez"

    again = client.post(
        f"/briefs/{brief_id}/decision",
        json={"action": "escalate", "actor": "J. Alvarez", "reason": "Reconsider."},
    )
    assert again.status_code == 409


def test_decision_validation(client):
    created = client.post("/briefs", json={"account_id": "ACC-002", "as_of": AS_OF})
    brief_id = created.json()["brief_id"]
    missing_actor = client.post(
        f"/briefs/{brief_id}/decision",
        json={"action": "proceed", "actor": "", "reason": "x"},
    )
    assert missing_actor.status_code == 422
    bad_action = client.post(
        f"/briefs/{brief_id}/decision",
        json={"action": "bind", "actor": "J", "reason": "x"},
    )
    assert bad_action.status_code == 422  # the agent never binds
    assert client.post("/briefs/brief-nope/decision",
                       json={"action": "proceed", "actor": "J", "reason": "x"}
                       ).status_code == 404


def test_console_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Zep Underwriting Context Assembly" in page.text
    assert "View all blueprints" in page.text
