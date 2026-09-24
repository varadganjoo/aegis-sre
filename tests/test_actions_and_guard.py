"""The action allowlist and the blast-radius guard."""

import pytest

from app import actions
from app.blast_radius import BlastRadiusGuard
from app.graph import get_default_topology
from app.schemas import ActionType, ClusterTopology, RemediationAction, RiskLevel, ServiceNode, ServiceType

TOPOLOGY = get_default_topology()


def act(action_type, target, rollback="Reverse the change if the alert does not clear.", **parameters):
    params = actions.validate_action(ActionType(action_type), target, parameters, TOPOLOGY)
    return RemediationAction(action_id="a", action_type=ActionType(action_type), target_service=target,
                             parameters=params, rationale="test", rollback_plan=rollback)


# ---------------------------------------------------------------- allowlist


def test_parameters_are_validated_and_normalized():
    assert actions.validate_action(ActionType.DRAIN, "payment-orchestrator", {}, TOPOLOGY) == {"timeout_seconds": 60}
    assert actions.validate_action(ActionType.SCALE, "api-gateway", {"target_replicas": 6}, TOPOLOGY) == {"target_replicas": 6}


@pytest.mark.parametrize("action_type, target, params, message", [
    (ActionType.SCALE, "api-gateway", {"target_replicas": 50}, "less than or equal to 20"),
    (ActionType.SCALE, "api-gateway", {}, "exactly one"),
    (ActionType.SCALE, "api-gateway", {"target_replicas": 4, "scale_factor": 2}, "exactly one"),
    (ActionType.DRAIN, "api-gateway", {"timeout_seconds": 60, "command": "anything"}, "Extra inputs"),
    (ActionType.CACHE_FLUSH, "checkout-service", {}, "only applies to stateful"),
    (ActionType.FAILOVER, "redis-cluster", {}, "only applies to databases"),
    (ActionType.RESTART, "billing-service", {}, "Unknown target service"),
])
def test_invalid_actions_are_rejected(action_type, target, params, message):
    with pytest.raises(actions.InvalidAction, match=message):
        actions.validate_action(action_type, target, params, TOPOLOGY)


def test_simulation_describes_without_touching_anything():
    before = TOPOLOGY.model_dump()
    log = actions.simulate(ActionType.SCALE, "api-gateway", {"scale_factor": 2.0}, TOPOLOGY)
    assert log == "[simulated] Scaled api-gateway from 4 to 8 replicas."
    assert TOPOLOGY.model_dump() == before


# ---------------------------------------------------------------- guard


def test_risk_levels_order_by_severity_not_alphabet():
    assert RiskLevel.highest(RiskLevel.LOW, RiskLevel.HIGH) == RiskLevel.HIGH
    assert RiskLevel.highest(RiskLevel.MEDIUM, RiskLevel.CRITICAL, RiskLevel.LOW) == RiskLevel.CRITICAL


def test_dependents_include_transitive_callers():
    assert BlastRadiusGuard.find_downstream_dependents("postgres-primary", TOPOLOGY) == {
        "auth-service", "payment-orchestrator", "inventory-service", "checkout-service", "api-gateway",
    }


def test_dependency_walk_survives_cycles():
    cyclic = ClusterTopology(services={
        "a": ServiceNode(name="a", type=ServiceType.SERVICE, dependencies=["b"]),
        "b": ServiceNode(name="b", type=ServiceType.SERVICE, dependencies=["a"]),
    })
    assert BlastRadiusGuard.find_downstream_dependents("a", cyclic) == {"b"}


def test_scaling_up_is_not_blocked_by_traffic_exposure():
    radius = BlastRadiusGuard.evaluate(act("scale_replicas", "api-gateway", scale_factor=2.0), TOPOLOGY)
    assert radius.requires_sre_approval is False
    assert radius.risk_level == RiskLevel.LOW


def test_disruptive_action_on_the_gateway_needs_approval():
    radius = BlastRadiusGuard.evaluate(act("restart_pods", "api-gateway"), TOPOLOGY)
    assert radius.requires_sre_approval and radius.risk_level == RiskLevel.CRITICAL


def test_moderate_traffic_raises_risk_to_high():
    topology = ClusterTopology(services={
        "worker": ServiceNode(name="worker", type=ServiceType.SERVICE, replicas=3),
        "other": ServiceNode(name="other", type=ServiceType.SERVICE, replicas=7),
    })
    action = RemediationAction(action_id="a", action_type=ActionType.RESTART, target_service="worker",
                               parameters={"max_unavailable": 1}, rationale="t", rollback_plan="Reverse it if needed.")
    radius = BlastRadiusGuard.evaluate(action, topology)
    assert radius.user_traffic_pct == 30.0
    assert radius.risk_level == RiskLevel.HIGH  # was LOW when levels were compared as strings


def test_stateful_restart_and_forced_failover_are_critical():
    assert BlastRadiusGuard.evaluate(act("restart_pods", "postgres-primary"), TOPOLOGY).risk_level == RiskLevel.CRITICAL
    forced = BlastRadiusGuard.evaluate(act("database_failover", "postgres-primary", mode="forced"), TOPOLOGY)
    assert any("Forced failover" in r for r in forced.reasons)


def test_flush_all_is_flagged_but_scan_delete_is_not():
    flush_all = BlastRadiusGuard.evaluate(act("flush_cache", "redis-cluster", mode="flush_all"), TOPOLOGY)
    scan = BlastRadiusGuard.evaluate(act("flush_cache", "redis-cluster", mode="scan_delete"), TOPOLOGY)
    assert any("Flushing all keys" in r for r in flush_all.reasons)
    assert not any("Flushing all keys" in r for r in scan.reasons)


def test_learned_invariant_and_novel_proposals_require_review():
    action = act("scale_replicas", "api-gateway", scale_factor=2.0)
    vetoed = BlastRadiusGuard.evaluate(action, TOPOLOGY, forbidden=[ActionType.SCALE])
    novel = BlastRadiusGuard.evaluate(action, TOPOLOGY, novel=True)
    assert vetoed.requires_sre_approval and vetoed.risk_level == RiskLevel.CRITICAL
    assert novel.requires_sre_approval and any("Novel diagnosis" in r for r in novel.reasons)


def test_missing_rollback_plan_is_critical():
    radius = BlastRadiusGuard.evaluate(act("scale_replicas", "api-gateway", rollback="n/a", scale_factor=2.0), TOPOLOGY)
    assert radius.risk_level == RiskLevel.CRITICAL


def test_scale_down_below_redundancy_is_flagged():
    radius = BlastRadiusGuard.evaluate(act("scale_replicas", "inventory-service", target_replicas=1), TOPOLOGY)
    assert any("N+1 redundancy" in r for r in radius.reasons)
