"""Shared fixtures. Tests run offline (no model keys) and in memory (no AEGIS_STATE_DIR), so they never touch the
network or write into the repository."""

import pytest

from app.graph import AegisRuntime
from app.schemas import DiagnosisProposal


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for var in ("GEMINI_API_KEY", "GROQ_API_KEY", "AEGIS_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def runtime():
    return AegisRuntime()


PAYMENT_ALERT = {
    "alert_id": "ALT-101", "service": "payment-orchestrator", "metric": "p99_latency_ms",
    "value": 1450.0, "threshold": 300.0, "severity": "critical", "description": "p99 latency spike",
}
GATEWAY_CPU_ALERT = {
    "alert_id": "ALT-103", "service": "api-gateway", "metric": "cpu_utilization_pct",
    "value": 89.2, "threshold": 75.0, "severity": "high", "description": "cpu spike",
}
POSTGRES_ALERT = {
    "alert_id": "ALT-102", "service": "postgres-primary", "metric": "connection_pool_usage_pct",
    "value": 98.4, "threshold": 80.0, "severity": "critical", "description": "pool exhausted",
}


def proposal(**overrides) -> DiagnosisProposal:
    base = dict(
        root_cause_hypothesis="Connection pool starvation on postgres-primary is backing up payment requests.",
        evidence=["payment-orchestrator depends on postgres-primary", "p99 latency 1450ms vs 300ms"],
        action_type="drain_traffic", target_service="postgres-primary", timeout_seconds=90,
        rationale="Shed load at the database instead of adding more connections.",
        rollback_plan="Restore routing and pool limits once latency recovers.", confidence=0.72,
    )
    return DiagnosisProposal(**{**base, **overrides})


@pytest.fixture
def llm_proposes(monkeypatch):
    """Makes the diagnosis model return the given proposal (or raise the given exception)."""

    def install(result):
        def fake(prompt, system_instruction, schema, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr("app.diagnosis.generate_structured", fake)

    return install
