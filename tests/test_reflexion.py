"""Unit tests for Reflexion & Experiential Learning Memory."""

import pytest
from app.memory import ExperientialMemoryKernel
from app.reflexion import ReflexionEngine
from app.schemas import (
    ActionType,
    Alert,
    AlertSeverity,
    IncidentState,
    IncidentStatus,
    RemediationAction,
    RiskLevel,
)
from app.skill_bank import DynamicSkillBank


@pytest.fixture
def memory(tmp_path):
    return ExperientialMemoryKernel(data_file=tmp_path / "memory.json")


@pytest.fixture
def reflexion(tmp_path):
    bank = DynamicSkillBank(skills_dir=tmp_path / "skills")
    return ReflexionEngine(skill_bank=bank)


def test_initial_invariants_seeded(memory):
    """Verifies that baseline institutional SRE invariants are present."""
    invs = memory.query_invariants_for_service("postgres-primary")
    assert len(invs) > 0
    assert any("postgres-primary" in inv for inv in invs)


def test_reflexion_distills_override_and_synthesizes_skill(reflexion, memory):
    """Verifies that an SRE override triggers post-mortem invariant extraction and skill synthesis."""
    alert = Alert(
        alert_id="ALT-999",
        service="checkout-service",
        metric="504_gateway_timeout",
        value=15.0,
        threshold=1.0,
        severity=AlertSeverity.CRITICAL,
        description="Checkout gateway timeouts surging.",
    )

    state = IncidentState(
        incident_id="INC-999",
        title="Checkout Outage",
        alert=alert,
        status=IncidentStatus.AWAITING_APPROVAL,
        root_cause_hypothesis="Pod resource starvation",
        proposed_action=RemediationAction(
            action_id="act-999",
            action_type=ActionType.RESTART,
            target_service="checkout-service",
            rationale="Restart pods naively",
            rollback_plan="Wait for pod restart",
        ),
    )

    override_notes = "Do not restart checkout-service pods; drain traffic to region-east and clear stale Redis lock keys."
    override_action = "drain_traffic_and_clear_locks"

    trace, new_skill = reflexion.analyze_override(
        state=state,
        sre_override_notes=override_notes,
        actual_action_taken=override_action,
    )

    assert trace.was_overridden is True
    assert "Do not restart checkout-service pods" in trace.extracted_invariant
    assert new_skill is not None
    assert new_skill.name == "learned_sre_mitigation_checkout_service"

    # Record into memory
    state.reflexion = trace
    memory.record_episode(state)

    # Check that the invariant is now saved in institutional memory
    learned_invs = memory.query_invariants_for_service("checkout-service")
    assert len(learned_invs) >= 1
    assert any("drain traffic to region-east" in inv for inv in learned_invs)
