"""Deterministic blast-radius analysis and safety rules, applied to every proposed action before execution."""

from typing import Iterable, List, Set

from app.actions import resolved_replicas
from app.schemas import ActionType, BlastRadius, ClusterTopology, RemediationAction, RiskLevel


class BlastRadiusGuard:
    # Tier-1 services whose disruption affects most end-user journeys.
    TIER1_SERVICES = {"api-gateway", "auth-service", "checkout-service", "payment-orchestrator"}

    @classmethod
    def find_downstream_dependents(cls, target_service: str, topology: ClusterTopology) -> Set[str]:
        """Every service that depends on target_service, directly or transitively (cycle-safe)."""
        dependents: Set[str] = set()
        frontier = [target_service]
        while frontier:
            current = frontier.pop()
            for name, node in topology.services.items():
                if current in node.dependencies and name not in dependents and name != target_service:
                    dependents.add(name)
                    frontier.append(name)
        return dependents

    @classmethod
    def evaluate(
        cls,
        action: RemediationAction,
        topology: ClusterTopology,
        forbidden: Iterable[ActionType] = (),
        novel: bool = False,
    ) -> BlastRadius:
        """`novel` marks proposals that did not come from a verified skill (LLM or fallback rules)."""
        reasons: List[str] = []
        target = action.target_service
        node = topology.services.get(target)
        if node is None:
            return BlastRadius(
                impacted_services=[target], impacted_pods_count=0, total_pods_count=0, user_traffic_pct=0.0,
                risk_level=RiskLevel.CRITICAL, requires_sre_approval=True,
                reasons=[f"Target service '{target}' is not in the registered topology."],
            )

        downstream = cls.find_downstream_dependents(target, topology)
        impacted = sorted({target} | downstream)
        total_pods = sum(s.replicas for s in topology.services.values())
        impacted_pods = sum(topology.services[s].replicas for s in impacted)

        if target == "api-gateway" or "api-gateway" in downstream:
            traffic_pct = 100.0
            reasons.append("API gateway in the impact path: all external traffic is exposed.")
        elif target in cls.TIER1_SERVICES or downstream & cls.TIER1_SERVICES:
            traffic_pct = 75.0
            reasons.append("Tier-1 service in the impact path: most user journeys are exposed.")
        else:
            traffic_pct = min(100.0, round(impacted_pods / max(1, total_pods) * 100.0, 1))

        risk = RiskLevel.LOW
        needs_approval = False

        def flag(level: RiskLevel, reason: str) -> None:
            nonlocal risk, needs_approval
            risk = RiskLevel.highest(risk, level)
            needs_approval = True
            reasons.append(reason)

        if novel:
            flag(RiskLevel.MEDIUM, "Novel diagnosis rather than a verified skill: an SRE reviews it before anything runs.")

        if action.action_type in set(forbidden):
            flag(RiskLevel.CRITICAL, f"Learned invariant forbids {action.action_type.value} on {target}.")

        if node.is_stateful and action.action_type in {ActionType.RESTART, ActionType.FAILOVER}:
            flag(RiskLevel.CRITICAL, f"{target} holds persistent state; restarts and failovers risk data loss or split-brain.")
        if action.action_type == ActionType.FAILOVER and action.parameters.get("mode") == "forced":
            flag(RiskLevel.CRITICAL, "Forced failover skips replication catch-up and can lose committed writes.")
        if action.action_type == ActionType.CACHE_FLUSH and action.parameters.get("mode") == "flush_all":
            flag(RiskLevel.HIGH, f"Flushing all keys on {target} sends every read to the backing store at once.")

        # Adding capacity disrupts no one; every other action can interrupt the traffic flowing through the target.
        scaling_up = action.action_type == ActionType.SCALE and resolved_replicas(action.parameters, node.replicas) >= node.replicas
        if not scaling_up:
            if traffic_pct >= 50.0:
                flag(RiskLevel.CRITICAL, f"High traffic impact ({traffic_pct}% of users).")
            elif traffic_pct >= 25.0:
                flag(RiskLevel.HIGH, f"Moderate traffic impact ({traffic_pct}% of users).")

        if action.action_type == ActionType.SCALE:
            replicas = resolved_replicas(action.parameters, node.replicas)
            if replicas < 2 <= node.replicas:
                flag(RiskLevel.HIGH, f"Scaling {target} to {replicas} replica(s) removes N+1 redundancy.")
        if action.action_type == ActionType.CIRCUIT_BREAK:
            flag(RiskLevel.HIGH, f"Circuit breaking {target} fails requests from {len(downstream)} dependent service(s).")

        if len(action.rollback_plan.strip()) < 10:
            flag(RiskLevel.CRITICAL, "No meaningful rollback plan.")

        return BlastRadius(
            impacted_services=impacted,
            impacted_pods_count=impacted_pods,
            total_pods_count=total_pods,
            user_traffic_pct=traffic_pct,
            risk_level=risk,
            requires_sre_approval=needs_approval,
            reasons=reasons,
        )
