"""HTTP API (diagnose, decisions, validation) and the MCP server's read-only tools."""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.graph import AegisRuntime


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "runtime", AegisRuntime())
    return TestClient(main.app)


def diagnose(client, incident="INC-8491"):
    response = client.post(f"/api/incidents/{incident}/diagnose")
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_endpoints(client):
    assert {i["incident_id"] for i in client.get("/api/incidents").json()} == {"INC-8491", "INC-8492", "INC-8493"}
    assert "api-gateway" in client.get("/api/topology").json()["services"]
    kinds = {a["action_type"]: a for a in client.get("/api/actions").json()}
    assert kinds["flush_cache"]["stateful_only"] is True and "timeout_seconds" in kinds["drain_traffic"]["parameters"]
    assert {s["name"] for s in client.get("/api/skills").json()} >= {"horizontal_pod_scaler", "circuit_breaker_tripper"}
    health = client.get("/health").json()
    assert health["models"] == [] and health["backup"] is None  # offline in tests


def test_known_signature_resolves_without_a_gate(client):
    body = diagnose(client, "INC-8493")
    assert body["cognition_mode"] == "system1" and body["status"] == "resolved" and not body["awaiting_approval"]


def test_novel_incident_waits_then_override_learns(client):
    body = diagnose(client)
    assert body["awaiting_approval"] and body["thread_id"].startswith("INC-8491-")
    decided = client.post("/api/incidents/INC-8491/resume", json={
        "thread_id": body["thread_id"], "decision": "override", "notes": "DB pool starvation.",
        "override": {"action_type": "drain_traffic", "target_service": "postgres-primary", "parameters": {"timeout_seconds": 120}},
    }).json()
    assert decided["status"] == "overridden_by_sre"
    assert decided["learned_skill"]["target_service"] == "postgres-primary"
    assert client.get("/api/memory/episodes").json()[0]["incident_id"] == "INC-8491"
    assert any(i["rule"] == "DB pool starvation." for i in client.get("/api/memory/invariants").json())


def test_each_diagnosis_gets_its_own_thread(client):
    assert diagnose(client)["thread_id"] != diagnose(client)["thread_id"]


@pytest.mark.parametrize("override, fragment", [
    ({"action_type": "scale_replicas", "target_service": "api-gateway", "parameters": {"target_replicas": 500}}, "less than or equal to 20"),
    ({"action_type": "drain_traffic", "target_service": "api-gateway", "parameters": {"script": "x"}}, "Extra inputs"),
    ({"action_type": "flush_cache", "target_service": "checkout-service", "parameters": {}}, "only applies to stateful"),
    ({"action_type": "run_shell", "target_service": "api-gateway", "parameters": {}}, "action_type"),
])
def test_invalid_overrides_are_rejected_and_the_run_stays_paused(client, override, fragment):
    body = diagnose(client)
    response = client.post("/api/incidents/INC-8491/resume", json={"thread_id": body["thread_id"], "decision": "override", "override": override})
    assert response.status_code == 422 and fragment in response.text
    retry = client.post("/api/incidents/INC-8491/resume", json={"thread_id": body["thread_id"], "decision": "approve"})
    assert retry.status_code == 200 and retry.json()["status"] == "resolved"


def test_override_without_an_action_is_rejected(client):
    body = diagnose(client)
    assert client.post("/api/incidents/INC-8491/resume", json={"thread_id": body["thread_id"], "decision": "override"}).status_code == 422


def test_deciding_twice_or_on_the_wrong_incident_fails(client):
    body = diagnose(client)
    first = client.post("/api/incidents/INC-8491/resume", json={"thread_id": body["thread_id"], "decision": "reject"})
    assert first.status_code == 200 and first.json()["status"] == "rejected_by_sre"
    assert client.post("/api/incidents/INC-8491/resume", json={"thread_id": body["thread_id"], "decision": "approve"}).status_code == 409
    assert client.post("/api/incidents/INC-8492/resume", json={"thread_id": body["thread_id"], "decision": "approve"}).status_code == 404
    assert client.post("/api/incidents/NOPE/diagnose").status_code == 404


def test_mcp_tools_are_read_only_and_validated():
    from mcp_server import server

    assert "api-gateway" in json.loads(server.get_topology_resource())["services"]
    assert len(json.loads(server.get_invariants_resource())) >= 3
    assert {a["action_type"] for a in server.list_allowed_actions()} >= {"scale_replicas", "drain_traffic"}
    radius = server.calculate_blast_radius("postgres-primary", "restart_pods", "Reverse it if needed.")
    assert radius["requires_sre_approval"] and radius["risk_level"] == "critical"
    assert "error" in server.calculate_blast_radius("api-gateway", "run_shell", "x")
    assert "error" in server.calculate_blast_radius("api-gateway", "scale_replicas", "Reverse it.", {"target_replicas": 99})
    assert not hasattr(server, "synthesize_procedural_skill") and not hasattr(server, "execute_skill")
