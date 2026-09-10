"""Deterministic Blast-Radius Analysis & Safety Invariants Engine.
Enforces strict infrastructure guardrails before any remediation action is executed.
"""

from typing import Dict, List, Set
from app.schemas import (
    ActionType,
    BlastRadius,
    ClusterTopology,
    RemediationAction,
    RiskLevel,
    ServiceNode,
)


class BlastRadiusGuard:
    """Calculates blast radius and enforces deterministic safety invariants."""

    # Critical infrastructure nodes that carry persistent state
    STATEFUL_SERVICES = {"postgres-primary", "postgres-replica", "redis-cluster", "kafka-broker"}
    
    # Tier-1 services whose disruption affects > 50% of end-user traffic
    TIER1_SERVICES = {"api-gateway", "auth-service", "checkout-service", "payment-orchestrator"}

    @classmethod
    def find_downstream_dependents(cls, target_service: str, topology: ClusterTopology) -> Set[str]:
        """Traverses the dependency DAG to find all services that depend on target_service."""
        dependents = set()
        for s_name, s_node in topology.services.items():
            if target_service in s_node.dependencies:
                dependents.add(s_name)
                # Recursively add services depending on this dependent
                dependents.update(cls.find_downstream_dependents(s_name, topology))
        return dependents

    @classmethod
    def evaluate(cls, action: RemediationAction, topology: ClusterTopology) -> BlastRadius:
        """Evaluates the blast radius of a proposed remediation action."""
        reasons: List[str] = []
        target_name = action.target_service
        target_node = topology.services.get(target_name)

        if not target_node:
            return BlastRadius(
                impacted_services=[target_name],
                impacted_pods_count=1,
                total_pods_count=10,
                user_traffic_pct=10.0,
                risk_level=RiskLevel.MEDIUM,
                requires_sre_approval=True,
                reasons=[f"Target service '{target_name}' not found in registered topology."],
            )

        # 1. Calculate downstream impacted services
        downstream = cls.find_downstream_dependents(target_name, topology)
        all_impacted_services = sorted(list({target_name} | downstream))

        # 2. Calculate pod counts
        total_pods = sum(s.replicas for s in topology.services.values())
        impacted_pods = target_node.replicas + sum(
            topology.services[s].replicas for s in downstream if s in topology.services
        )

        # 3. Calculate user traffic exposure
        if target_name == "api-gateway" or "api-gateway" in downstream:
            traffic_pct = 100.0
            reasons.append("API Gateway is impacted; 100% of external traffic affected.")
        elif target_name in cls.TIER1_SERVICES or any(s in cls.TIER1_SERVICES for s in downstream):
            traffic_pct = 75.0
            reasons.append(f"Tier-1 service ({target_name}) impacted; estimated 75% of user journeys affected.")
        else:
            traffic_pct = min(100.0, round((impacted_pods / max(1, total_pods)) * 100.0, 1))

        # 4. Enforce Deterministic Safety Invariants
        risk_level = RiskLevel.LOW
        requires_approval = False

        # Invariant 1: Stateful service protection
        if target_node.is_stateful or target_name in cls.STATEFUL_SERVICES:
            if action.action_type in {ActionType.RESTART, ActionType.FAILOVER, ActionType.CUSTOM_SKILL}:
                risk_level = RiskLevel.CRITICAL
                requires_approval = True
                reasons.append(
                    f"CRITICAL SAFETY INVARIANT: Target '{target_name}' carries persistent state. "
                    "Restarts or manual failovers risk database split-brain or data corruption."
                )

        # Invariant 2: Traffic threshold
        if traffic_pct >= 50.0:
            risk_level = RiskLevel.CRITICAL
            requires_approval = True
            reasons.append(f"High traffic impact ({traffic_pct}% >= 50.0%). Requires SRE authorization.")
        elif traffic_pct >= 25.0:
            risk_level = max(risk_level, RiskLevel.HIGH)
            requires_approval = True
            reasons.append(f"Moderate traffic impact ({traffic_pct}% >= 25.0%). Requires SRE authorization.")

        # Invariant 3: Scale down below minimum redundancy
        if action.action_type == ActionType.SCALE:
            target_replicas = action.parameters.get("target_replicas", target_node.replicas)
            if target_replicas < 2 and target_node.replicas >= 2:
                risk_level = max(risk_level, RiskLevel.HIGH)
                requires_approval = True
                reasons.append(
                    f"Scaling '{target_name}' to {target_replicas} violates N+1 high-availability redundancy."
                )

        # Invariant 4: Destructive action without rollback plan
        if not action.rollback_plan or len(action.rollback_plan.strip()) < 10:
            risk_level = RiskLevel.CRITICAL
            requires_approval = True
            reasons.append("Remediation action lacks a detailed rollback plan. Blocked by policy.")

        # If action type is dangerous
        if action.action_type in {ActionType.CIRCUIT_BREAK, ActionType.FAILOVER}:
            risk_level = RiskLevel.CRITICAL
            requires_approval = True

        return BlastRadius(
            impacted_services=all_impacted_services,
            impacted_pods_count=impacted_pods,
            total_pods_count=total_pods,
            user_traffic_pct=traffic_pct,
            risk_level=risk_level,
            requires_sre_approval=requires_approval,
            reasons=reasons,
        )
