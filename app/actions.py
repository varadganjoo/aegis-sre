"""The remediation action allowlist: per-action parameter schemas, validation, and a simulated executor.

Everything an operator, a learned skill, or the LLM proposes is checked here before it can reach the
blast-radius guard. Unknown fields, out-of-range values, and targets outside the topology are rejected.
Execution is simulated: this demo has no real cluster, so "executing" produces a log line only.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas import ActionType, ClusterTopology, DiagnosisProposal


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScaleParams(_Params):
    target_replicas: Optional[int] = Field(None, ge=1, le=20)
    scale_factor: Optional[float] = Field(None, ge=1.1, le=3.0)

    @model_validator(mode="after")
    def exactly_one(self) -> "ScaleParams":
        if (self.target_replicas is None) == (self.scale_factor is None):
            raise ValueError("scale_replicas needs exactly one of target_replicas or scale_factor")
        return self


class RestartParams(_Params):
    max_unavailable: int = Field(1, ge=1, le=3)


class DrainParams(_Params):
    timeout_seconds: int = Field(60, ge=10, le=600)


class CircuitBreakParams(_Params):
    duration_seconds: int = Field(300, ge=30, le=3600)


class CacheFlushParams(_Params):
    mode: Literal["scan_delete", "flush_all"] = "scan_delete"


class FailoverParams(_Params):
    mode: Literal["planned_switchover", "forced"] = "planned_switchover"


PARAMS: Dict[ActionType, Type[_Params]] = {
    ActionType.SCALE: ScaleParams,
    ActionType.RESTART: RestartParams,
    ActionType.DRAIN: DrainParams,
    ActionType.CIRCUIT_BREAK: CircuitBreakParams,
    ActionType.CACHE_FLUSH: CacheFlushParams,
    ActionType.FAILOVER: FailoverParams,
}

# Which service kinds each action makes sense for.
STATEFUL_ONLY = {ActionType.CACHE_FLUSH, ActionType.FAILOVER}


class InvalidAction(ValueError):
    """An action type, target, or parameter set that is not allowed."""


def validate_action(action_type: ActionType, target: str, parameters: Dict[str, Any], topology: ClusterTopology) -> Dict[str, Any]:
    """Returns normalized parameters, or raises InvalidAction explaining what is wrong."""
    node = topology.services.get(target)
    if node is None:
        raise InvalidAction(f"Unknown target service '{target}'.")
    if action_type in STATEFUL_ONLY and not node.is_stateful:
        raise InvalidAction(f"{action_type.value} only applies to stateful services; '{target}' is stateless.")
    if action_type == ActionType.FAILOVER and node.type.value != "database":
        raise InvalidAction(f"database_failover only applies to databases; '{target}' is a {node.type.value}.")
    try:
        return PARAMS[action_type](**parameters).model_dump(exclude_none=True)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'parameters'}: {e['msg']}" for e in exc.errors())
        raise InvalidAction(f"Invalid parameters for {action_type.value}: {details}") from exc


def parameters_from_proposal(proposal: DiagnosisProposal) -> Dict[str, Any]:
    """Picks the flat proposal fields that belong to the proposed action type."""
    fields = {
        ActionType.SCALE: {"target_replicas": proposal.target_replicas, "scale_factor": proposal.scale_factor},
        ActionType.RESTART: {"max_unavailable": proposal.max_unavailable},
        ActionType.DRAIN: {"timeout_seconds": proposal.timeout_seconds},
        ActionType.CIRCUIT_BREAK: {"duration_seconds": proposal.duration_seconds},
        ActionType.CACHE_FLUSH: {"mode": proposal.cache_flush_mode},
        ActionType.FAILOVER: {"mode": proposal.failover_mode},
    }[proposal.action_type]
    return {k: v for k, v in fields.items() if v is not None}


def resolved_replicas(parameters: Dict[str, Any], current: int) -> int:
    if "target_replicas" in parameters:
        return parameters["target_replicas"]
    return min(20, max(current + 1, round(current * parameters["scale_factor"])))


def simulate(action_type: ActionType, target: str, parameters: Dict[str, Any], topology: ClusterTopology) -> str:
    """What executing the action would do. No real system is touched."""
    node = topology.services[target]
    if action_type == ActionType.SCALE:
        return f"[simulated] Scaled {target} from {node.replicas} to {resolved_replicas(parameters, node.replicas)} replicas."
    if action_type == ActionType.RESTART:
        return f"[simulated] Rolling restart of {target}, {parameters['max_unavailable']} pod(s) at a time."
    if action_type == ActionType.DRAIN:
        return f"[simulated] Drained traffic from {target} with a {parameters['timeout_seconds']}s connection timeout."
    if action_type == ActionType.CIRCUIT_BREAK:
        return f"[simulated] Tripped circuit breakers in front of {target} for {parameters['duration_seconds']}s."
    if action_type == ActionType.CACHE_FLUSH:
        return f"[simulated] Flushed {target} using {parameters['mode'].replace('_', ' ')}."
    return f"[simulated] {parameters['mode'].replace('_', ' ').capitalize()} of {target} to its replica."
