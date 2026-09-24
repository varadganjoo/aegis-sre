"""Skills as data: threshold and scope matching, invariant vetoes, and learning from SRE decisions."""

import json

import pytest
from langgraph.types import Command

from app import actions
from app.graph import AegisRuntime, get_default_topology
from app.memory import ExperientialMemoryKernel
from app.schemas import ActionType, Alert, Condition
from app.skill_bank import DynamicSkillBank
from tests.conftest import GATEWAY_CPU_ALERT, PAYMENT_ALERT


def alert(**overrides):
    return Alert(**{**GATEWAY_CPU_ALERT, **overrides})


def test_builtin_skill_matches_only_above_its_threshold(runtime):
    bank = runtime.skill_bank
    assert bank.match_skill_for_alert(alert(value=89.2)).name == "horizontal_pod_scaler"
    assert bank.match_skill_for_alert(alert(value=70.0)) is None


def test_stateless_only_skills_skip_stateful_services(runtime):
    assert runtime.skill_bank.match_skill_for_alert(alert(service="redis-cluster")) is None


def test_learned_skill_is_scoped_to_its_service(runtime):
    runtime.skill_bank.register_skill(
        name="learned", description="d", services=["payment-orchestrator"],
        conditions=[Condition(metric="p99_latency_ms", threshold=300.0)],
        action_type=ActionType.DRAIN, parameters={}, target_service="postgres-primary",
    )
    assert runtime.skill_bank.match_skill_for_alert(Alert(**PAYMENT_ALERT)).name == "learned"
    # Same metric on another service must not trigger it (the old substring matcher did).
    assert runtime.skill_bank.match_skill_for_alert(Alert(**{**PAYMENT_ALERT, "service": "inventory-service"})) is None


def test_invariants_veto_matching_skills(runtime):
    assert runtime.skill_bank.match_skill_for_alert(alert(), forbidden={ActionType.SCALE}) is None


def test_registering_a_skill_validates_its_action(runtime):
    with pytest.raises(actions.InvalidAction):
        runtime.skill_bank.register_skill(
            name="bad", description="d", services=["api-gateway"],
            conditions=[Condition(metric="cpu_utilization_pct", threshold=80.0)],
            action_type=ActionType.SCALE, parameters={"target_replicas": 999},
        )


def test_state_persists_only_when_a_directory_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_STATE_DIR", str(tmp_path))
    topology = get_default_topology()
    bank = DynamicSkillBank(topology)
    bank.register_skill(name="kept", description="d", services=["api-gateway"],
                        conditions=[Condition(metric="cpu_utilization_pct", threshold=90.0)],
                        action_type=ActionType.SCALE, parameters={"scale_factor": 1.5})
    assert "kept" in [s["name"] for s in json.loads((tmp_path / "skills.json").read_text())]
    assert DynamicSkillBank(topology).get_skill("kept") is not None

    monkeypatch.delenv("AEGIS_STATE_DIR")
    assert DynamicSkillBank(topology).get_skill("kept") is None
    assert ExperientialMemoryKernel().state_file is None


# ---------------------------------------------------------------- learning through the graph


def _run_to_gate(rt: AegisRuntime, thread: str, alert_payload=PAYMENT_ALERT):
    config = {"configurable": {"thread_id": thread}}
    state = rt.graph.invoke({"incident_id": "INC-1", "alert": alert_payload}, config)
    assert state.get("__interrupt__"), state.get("steps")
    return config


OVERRIDE = {
    "action_id": "o", "action_type": "drain_traffic", "target_service": "postgres-primary",
    "parameters": {"timeout_seconds": 120}, "rationale": "sre", "rollback_plan": "Restore pool limits afterwards.",
}


def test_override_learns_an_invariant_and_a_skill_that_is_used_next_time(runtime):
    config = _run_to_gate(runtime, "t1")
    notes = 'Pool starvation, not load. Quotes " and \\ and newlines\nare stored as text only.'
    state = runtime.graph.invoke(Command(resume={"decision": "override", "override": OVERRIDE, "notes": notes}), config)

    assert state["status"] == "overridden_by_sre"
    assert state["execution_result"].startswith("[simulated] Drained traffic from postgres-primary")
    assert state["reflexion"].forbidden_actions == [ActionType.SCALE]
    skill = state["learned_skill"]
    assert (skill.action_type, skill.target_service, skill.created_by) == (ActionType.DRAIN, "postgres-primary", "sre-override")
    assert any(inv.rule == notes for inv in runtime.memory.invariants)   # verbatim data, never code

    again = runtime.graph.invoke({"incident_id": "INC-2", "alert": PAYMENT_ALERT}, {"configurable": {"thread_id": "t2"}})
    assert again["cognition_mode"] == "system1"
    assert again["proposed_action"].skill_name == skill.name


def test_parameter_tweak_does_not_forbid_the_action_type(runtime):
    config = _run_to_gate(runtime, "t3")
    proposed = runtime.graph.get_state(config).values["proposed_action"]
    tweak = {**OVERRIDE, "action_type": proposed.action_type.value, "target_service": proposed.target_service,
             "parameters": {"target_replicas": 3}}
    state = runtime.graph.invoke(Command(resume={"decision": "override", "override": tweak, "notes": None}), config)
    assert state["reflexion"].forbidden_actions == []


def test_rejection_forbids_the_action_and_executes_nothing(runtime):
    config = _run_to_gate(runtime, "t4")
    state = runtime.graph.invoke(Command(resume={"decision": "reject", "override": None, "notes": "Not during peak."}), config)
    assert state["status"] == "rejected_by_sre"
    assert state["execution_result"] == "Rejected by SRE; nothing was executed."
    assert state["learned_skill"] is None
    assert ActionType.SCALE in runtime.memory.forbidden_actions_for("payment-orchestrator")


def test_approval_executes_the_proposal(runtime):
    config = _run_to_gate(runtime, "t5")
    state = runtime.graph.invoke(Command(resume={"decision": "approve", "override": None, "notes": None}), config)
    assert state["status"] == "resolved" and state["execution_result"].startswith("[simulated]")
    assert runtime.memory.episodes[-1]["outcome"] == "resolved"
