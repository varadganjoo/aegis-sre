"""Unit tests for Model Context Protocol (MCP) Server."""

import json
import pytest
from mcp_server.server import (
    calculate_blast_radius,
    execute_skill,
    get_invariants_resource,
    get_skills_resource,
    get_topology_resource,
    query_service_health,
    synthesize_procedural_skill,
)


def test_mcp_resources():
    """Verifies that MCP resources return valid JSON payloads."""
    topo_json = get_topology_resource()
    topo = json.loads(topo_json)
    assert "services" in topo
    assert "api-gateway" in topo["services"]

    skills_json = get_skills_resource()
    skills = json.loads(skills_json)
    assert isinstance(skills, list)
    assert len(skills) >= 2

    invs_json = get_invariants_resource()
    invs = json.loads(invs_json)
    assert isinstance(invs, list)
    assert len(invs) >= 3


def test_mcp_query_service_health():
    """Verifies MCP tool querying service health."""
    res = query_service_health("auth-service")
    assert res["name"] == "auth-service"
    assert res["status"] == "healthy"

    res_err = query_service_health("non-existent-svc")
    assert "error" in res_err


def test_mcp_calculate_blast_radius():
    """Verifies MCP tool calculating blast radius for proposed action."""
    res = calculate_blast_radius(
        target_service="postgres-primary",
        action_type="restart_pod",
        rollback_plan="Graceful replica promotion",
    )
    assert res["risk_level"] == "critical"
    assert res["requires_sre_approval"] is True


def test_mcp_execute_skill():
    """Verifies MCP tool executing a verified procedural skill."""
    res = execute_skill("horizontal_pod_scaler", "checkout-service")
    assert res["success"] is True
    assert res["action"] == "scale_replicas"


def test_mcp_synthesize_procedural_skill():
    """Verifies MCP tool synthesizing, sandboxing, and registering a new skill."""
    code = """
def run(ctx):
    return {"success": True, "action": "custom_cleanup"}
"""
    test_code = """
def test():
    res = run({})
    assert res["success"] is True
"""
    res = synthesize_procedural_skill(
        name="mcp_test_cleanup_skill",
        description="MCP test cleanup",
        trigger_pattern="cleanup AND test",
        code=code,
        test_code=test_code,
    )
    assert res["success"] is True
    assert res["skill"]["name"] == "mcp_test_cleanup_skill"
