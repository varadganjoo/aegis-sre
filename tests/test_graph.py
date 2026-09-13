"""Unit tests for LangGraph StateGraph (System 1 vs System 2, interrupt, and resume)."""

import pytest
from langgraph.types import Command
from app.graph import build_aegis_graph
from app.schemas import Alert, AlertSeverity


@pytest.fixture
def graph():
    return build_aegis_graph()


def test_system1_fast_path_bypasses_deliberate_reasoning(graph):
    """Verifies that an alert matching a pre-learned skill takes the System 1 fast path."""
    alert = Alert(
        alert_id="ALT-SYS1-01",
        service="checkout-service",
        metric="cpu_utilization_pct",
        value=92.0,
        threshold=80.0,
        severity=AlertSeverity.HIGH,
        description="Stateless CPU spike matching horizontal_pod_scaler trigger.",
    )

    config = {"configurable": {"thread_id": "test-thread-sys1"}}
    initial_state = {"incident_id": "INC-SYS1", "alert": alert}

    # Run graph
    result = graph.invoke(initial_state, config=config)
    assert result.get("cognition_mode") == "system1_fast_path"
    assert "horizontal_pod_scaler" in result["proposed_action"].parameters.get("skill_name", "")


def test_system2_pauses_at_sre_review_gate(graph):
    """Verifies that a high-risk or stateful incident engages System 2 and pauses at interrupt()."""
    alert = Alert(
        alert_id="ALT-SYS2-01",
        service="postgres-primary",
        metric="connection_pool_usage_pct",
        value=99.0,
        threshold=80.0,
        severity=AlertSeverity.CRITICAL,
        description="Postgres connection pool exhausted; stateful database.",
    )

    config = {"configurable": {"thread_id": "test-thread-sys2"}}
    initial_state = {"incident_id": "INC-SYS2", "alert": alert}

    # Run graph; should pause at interrupt()
    graph.invoke(initial_state, config=config)

    state_snap = graph.get_state(config)
    assert state_snap.next == ("sre_review_gate",)
    assert len(state_snap.tasks) > 0
    assert len(state_snap.tasks[0].interrupts) > 0

    interrupt_data = state_snap.tasks[0].interrupts[0].value
    assert interrupt_data["blast_radius"]["requires_sre_approval"] is True


def test_graph_resumes_with_sre_override_and_learns(graph):
    """Verifies that resuming with an SRE override executes the override and updates memory."""
    alert = Alert(
        alert_id="ALT-OVERRIDE-01",
        service="payment-orchestrator",
        metric="p99_latency_ms",
        value=1800.0,
        threshold=300.0,
        severity=AlertSeverity.CRITICAL,
        description="Payment orchestrator p99 latency surge.",
    )

    config = {"configurable": {"thread_id": "test-thread-override"}}
    initial_state = {"incident_id": "INC-OVERRIDE", "alert": alert}

    # First run: pause at interrupt
    graph.invoke(initial_state, config=config)

    # Resume with SRE override
    resume_payload = {
        "approved": False,
        "override_notes": "Trip upstream payment circuit breaker and route transactions to secondary processor.",
        "override_action": "trip_circuit_breaker_and_reroute",
    }

    result = graph.invoke(Command(resume=resume_payload), config=config)
    assert result.get("status") == "overridden_by_sre"
    assert "SRE Overrode proposal" in result.get("execution_result", "")
    assert result.get("reflexion") is not None
    assert result["reflexion"].was_overridden is True
    assert result.get("synthesized_skill") is not None
