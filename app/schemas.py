"""Data models for Aegis-SRE: telemetry, topology, alerts, remediation actions, skills, invariants, and reflexion.

Remediation is data, never code: every action is one of a fixed set of action types with validated parameters
(see app/actions.py), and learned skills are records that pair trigger conditions with such an action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceType(str, Enum):
    GATEWAY = "gateway"
    SERVICE = "microservice"
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"


class ServiceStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    MAINTENANCE = "maintenance"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["low", "medium", "high", "critical"].index(self.value)

    @classmethod
    def highest(cls, *levels: "RiskLevel") -> "RiskLevel":
        # Enum values are strings; max() on them would compare alphabetically ("low" > "high").
        return max(levels, key=lambda level: level.rank)


class IncidentStatus(str, Enum):
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_sre_approval"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    OVERRIDDEN = "overridden_by_sre"
    REJECTED = "rejected_by_sre"


class ActionType(str, Enum):
    """The only remediations Aegis can propose or execute."""

    SCALE = "scale_replicas"
    RESTART = "restart_pods"
    DRAIN = "drain_traffic"
    CIRCUIT_BREAK = "trip_circuit_breaker"
    CACHE_FLUSH = "flush_cache"
    FAILOVER = "database_failover"


class ServiceNode(BaseModel):
    name: str
    type: ServiceType
    replicas: int = 1
    ready_replicas: int = 1
    status: ServiceStatus = ServiceStatus.HEALTHY
    dependencies: List[str] = Field(default_factory=list)
    is_stateful: bool = False


class ClusterTopology(BaseModel):
    cluster_id: str = "demo-cluster"
    services: Dict[str, ServiceNode] = Field(default_factory=dict)


class Alert(BaseModel):
    alert_id: str
    service: str
    metric: str
    value: float
    threshold: float
    severity: AlertSeverity
    description: str
    timestamp: str = Field(default_factory=_now)


class BlastRadius(BaseModel):
    impacted_services: List[str]
    impacted_pods_count: int
    total_pods_count: int
    user_traffic_pct: float
    risk_level: RiskLevel
    requires_sre_approval: bool
    reasons: List[str] = Field(default_factory=list)


class RemediationAction(BaseModel):
    action_id: str
    action_type: ActionType
    target_service: str
    parameters: Dict[str, Any] = Field(default_factory=dict)  # validated per action type in app/actions.py
    rationale: str
    rollback_plan: str
    skill_name: Optional[str] = None


class Condition(BaseModel):
    """One trigger condition, evaluated against an alert's metric and value."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(..., min_length=1, max_length=64)
    comparator: Literal[">", ">=", "<", "<="] = ">="
    threshold: float

    def matches(self, metric: str, value: float) -> bool:
        if metric != self.metric:
            return False
        return {
            ">": value > self.threshold,
            ">=": value >= self.threshold,
            "<": value < self.threshold,
            "<=": value <= self.threshold,
        }[self.comparator]


class SkillDefinition(BaseModel):
    """A procedural skill: when these conditions hold on these services, propose this action."""

    skill_id: str
    name: str
    description: str
    services: List[str] = Field(default_factory=list)  # empty = any service (subject to stateless_only)
    stateless_only: bool = False
    conditions: List[Condition] = Field(..., min_length=1)  # any one matching condition triggers the skill
    action_type: ActionType
    parameters: Dict[str, Any] = Field(default_factory=dict)
    target_service: Optional[str] = None  # None = act on the alerting service
    created_by: Literal["builtin", "sre-override"] = "builtin"
    source_incident: Optional[str] = None
    success_count: int = 0
    created_at: str = Field(default_factory=_now)


class Invariant(BaseModel):
    """An institutional rule. `forbidden_actions` are enforced by the blast-radius guard; `rule` is context."""

    service: str
    rule: str
    forbidden_actions: List[ActionType] = Field(default_factory=list)
    source: str = "initial_sre_policy"
    created_at: str = Field(default_factory=_now)


class DiagnosisProposal(BaseModel):
    """Structured output requested from the LLM. Flat optional parameter fields keep the schema model-friendly;
    app/actions.py turns the relevant ones into validated action parameters."""

    root_cause_hypothesis: str
    evidence: List[str]
    action_type: ActionType
    target_service: str
    target_replicas: Optional[int] = None
    scale_factor: Optional[float] = None
    timeout_seconds: Optional[int] = None
    duration_seconds: Optional[int] = None
    max_unavailable: Optional[int] = None
    cache_flush_mode: Optional[Literal["scan_delete", "flush_all"]] = None
    failover_mode: Optional[Literal["planned_switchover", "forced"]] = None
    rationale: str
    rollback_plan: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class ReflexionTrace(BaseModel):
    incident_id: str
    alert_summary: str
    initial_hypothesis: str
    proposed_action: str
    outcome: Literal["overridden", "rejected"]
    human_override_reason: Optional[str] = None
    applied_action: Optional[str] = None
    extracted_invariant: str
    forbidden_actions: List[ActionType] = Field(default_factory=list)
    learned_skill_name: Optional[str] = None
    timestamp: str = Field(default_factory=_now)
