"""Unit tests for Deterministic Blast-Radius Analysis & Safety Guardrails."""

import pytest
from app.blast_radius import BlastRadiusGuard
from app.graph import get_default_topology
from app.schemas import ActionType, RemediationAction, RiskLevel


@pytest.fixture
def topology():
    return get_default_topology()


def test_find_downstream_dependents(topology):
    """Verifies recursive DAG traversal for downstream service impact."""
    # auth-service depends on postgres-primary and redis-cluster
    # api-gateway depends on auth-service, checkout-service, inventory-service
    deps = BlastRadiusGuard.find_downstream_dependents("postgres-primary", topology)
    assert "auth-service" in deps
    assert "payment-orchestrator" in deps
    assert "inventory-service" in deps
    assert "api-gateway" in deps  # Transitively dependent!


def test_blocks_stateful_service_restart(topology):
    """Safety Invariant: Stateful databases cannot be restarted directly without SRE authorization."""
    action = RemediationAction(
        action_id="act-db-01",
        action_type=ActionType.RESTART,
        target_service="postgres-primary",
        rationale="Attempted naive pod restart",
        rollback_plan="Restart pod again if it fails",
    )
    result = BlastRadiusGuard.evaluate(action, topology)
    assert result.risk_level == RiskLevel.CRITICAL
    assert result.requires_sre_approval is True
    assert any("persistent state" in r for r in result.reasons)


def test_detects_tier1_traffic_exposure(topology):
    """Safety Invariant: Disrupting api-gateway or tier-1 services affects major traffic."""
    action = RemediationAction(
        action_id="act-gw-01",
        action_type=ActionType.DRAIN,
        target_service="api-gateway",
        rationale="Drain traffic for maintenance",
        rollback_plan="Re-route traffic via fallback ingress controller",
    )
    result = BlastRadiusGuard.evaluate(action, topology)
    assert result.user_traffic_pct == 100.0
    assert result.requires_sre_approval is True


def test_blocks_violating_redundancy(topology):
    """Safety Invariant: Cannot scale stateless deployment below N+1 redundancy without approval."""
    action = RemediationAction(
        action_id="act-scale-01",
        action_type=ActionType.SCALE,
        target_service="checkout-service",
        parameters={"target_replicas": 1},
        rationale="Scale down to save compute cost",
        rollback_plan="Scale back to 4 replicas",
    )
    result = BlastRadiusGuard.evaluate(action, topology)
    assert result.requires_sre_approval is True
    assert any("N+1 high-availability" in r for r in result.reasons)


def test_blocks_destructive_action_without_rollback_plan(topology):
    """Safety Invariant: Missing or trivial rollback plan triggers critical block."""
    action = RemediationAction(
        action_id="act-norollback-01",
        action_type=ActionType.FAILOVER,
        target_service="redis-cluster",
        rationale="Failover cluster",
        rollback_plan="",  # Empty rollback plan!
    )
    result = BlastRadiusGuard.evaluate(action, topology)
    assert result.risk_level == RiskLevel.CRITICAL
    assert result.requires_sre_approval is True
    assert any("lacks a detailed rollback plan" in r for r in result.reasons)
