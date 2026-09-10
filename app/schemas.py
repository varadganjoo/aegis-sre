"""Data models and schemas for Aegis-SRE Autonomous Incident Response Platform.
Defines telemetry, topology DAG, alerts, blast radius, skills, and reflexion models.
"""

from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


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


class IncidentStatus(str, Enum):
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_sre_approval"
    MITIGATING = "mitigating"
    RESOLVED = "resolved"
    OVERRIDDEN = "overridden_by_sre"


class ActionType(str, Enum):
    SCALE = "scale_replicas"
    RESTART = "restart_pod"
    DRAIN = "drain_traffic"
    CIRCUIT_BREAK = "trip_circuit_breaker"
    CONFIG_PATCH = "patch_config"
    CACHE_FLUSH = "flush_cache"
    FAILOVER = "database_failover"
    CUSTOM_SKILL = "execute_custom_skill"


class ServiceNode(BaseModel):
    name: str
    type: ServiceType
    replicas: int = 1
    ready_replicas: int = 1
    status: ServiceStatus = ServiceStatus.HEALTHY
    cpu_utilization_pct: float = 25.0
    memory_utilization_pct: float = 35.0
    p99_latency_ms: float = 45.0
    error_rate_pct: float = 0.05
    dependencies: List[str] = Field(default_factory=list)
    is_stateful: bool = False


class ClusterTopology(BaseModel):
    cluster_id: str = "prod-us-east-1"
    services: Dict[str, ServiceNode] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Alert(BaseModel):
    alert_id: str
    service: str
    metric: str
    value: float
    threshold: float
    severity: AlertSeverity
    description: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


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
    parameters: Dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    rationale: str
    rollback_plan: str
    skill_name: Optional[str] = None


class SkillDefinition(BaseModel):
    skill_id: str
    name: str
    description: str
    trigger_pattern: str
    code: str
    test_code: str
    version: int = 1
    success_count: int = 0
    failure_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: str = "gemini-3.8-flash-synthesizer"


class ReflexionTrace(BaseModel):
    incident_id: str
    alert_summary: str
    initial_hypothesis: str
    proposed_action: str
    was_overridden: bool
    human_override_reason: Optional[str] = None
    applied_action: str
    root_cause_distillation: str
    extracted_invariant: str
    synthesized_skill_name: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class IncidentState(BaseModel):
    incident_id: str
    title: str
    alert: Alert
    status: IncidentStatus = IncidentStatus.TRIAGING
    cognition_mode: str = "system1_fast_path"  # "system1_fast_path" or "system2_deliberate"
    root_cause_hypothesis: Optional[str] = None
    investigation_steps: List[str] = Field(default_factory=list)
    proposed_action: Optional[RemediationAction] = None
    blast_radius: Optional[BlastRadius] = None
    synthesized_skill: Optional[SkillDefinition] = None
    sre_approved: Optional[bool] = None
    sre_override_notes: Optional[str] = None
    execution_result: Optional[str] = None
    reflexion: Optional[ReflexionTrace] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
