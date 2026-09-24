"""LangGraph incident workflow: triage, diagnosis, blast-radius guard, SRE review gate, simulated execution,
and reflexion.

    ingest -> system1 (learned/builtin skill?) --match--> guard
                                               --miss---> system2 (LLM, validated; rules fallback) -> guard
    guard -> sre_gate (interrupt() when approval is required) -> execute (simulated) -> reflexion -> END
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app import actions
from app.blast_radius import BlastRadiusGuard
from app.diagnosis import diagnose
from app.memory import ExperientialMemoryKernel
from app.reflexion import ReflexionEngine
from app.schemas import (
    Alert,
    ClusterTopology,
    IncidentStatus,
    RemediationAction,
    ServiceNode,
    ServiceType,
)
from app.skill_bank import DynamicSkillBank


class IncidentGraphState(TypedDict, total=False):
    """One channel per key, so each node returns only the keys it changes."""

    incident_id: str
    alert: Any
    status: str
    steps: List[str]
    cognition_mode: str
    proposed_action: Any
    root_cause_hypothesis: str
    evidence: List[str]
    confidence: Optional[float]
    diagnosis_note: str
    blast_radius: Any
    decision: str
    override: Any
    sre_notes: Optional[str]
    execution_result: str
    reflexion: Any
    learned_skill: Any


def get_default_topology() -> ClusterTopology:
    """A small microservices cluster: gateway -> services -> cache and database."""

    def svc(name, kind, replicas, deps=(), stateful=False):
        return ServiceNode(name=name, type=kind, replicas=replicas, ready_replicas=replicas, dependencies=list(deps), is_stateful=stateful)

    services = [
        svc("api-gateway", ServiceType.GATEWAY, 4, ["auth-service", "checkout-service", "inventory-service"]),
        svc("auth-service", ServiceType.SERVICE, 3, ["redis-cluster", "postgres-primary"]),
        svc("checkout-service", ServiceType.SERVICE, 4, ["payment-orchestrator", "redis-cluster", "inventory-service"]),
        svc("payment-orchestrator", ServiceType.SERVICE, 2, ["postgres-primary"]),
        svc("inventory-service", ServiceType.SERVICE, 3, ["postgres-primary"]),
        svc("redis-cluster", ServiceType.CACHE, 3, stateful=True),
        svc("postgres-primary", ServiceType.DATABASE, 1, stateful=True),
    ]
    return ClusterTopology(services={s.name: s for s in services})


class AegisRuntime:
    """One topology, skill bank, memory, and compiled graph. The app uses one; tests build fresh ones."""

    def __init__(self, topology: Optional[ClusterTopology] = None, skill_bank=None, memory=None):
        self.topology = topology or get_default_topology()
        self.skill_bank = skill_bank or DynamicSkillBank(self.topology)
        self.memory = memory or ExperientialMemoryKernel()
        self.reflexion = ReflexionEngine(self.skill_bank, self.memory)
        self.graph = self._build()

    # ---------------------------------------------------------------- nodes

    def ingest(self, state: Dict[str, Any]) -> Dict[str, Any]:
        alert = Alert(**state["alert"]) if isinstance(state["alert"], dict) else state["alert"]
        if alert.service not in self.topology.services:
            raise ValueError(f"Alert service '{alert.service}' is not in the topology.")
        return {
            "alert": alert,
            "status": IncidentStatus.INVESTIGATING.value,
            "steps": [f"Ingested {alert.alert_id}: {alert.service} {alert.metric}={alert.value} (threshold {alert.threshold})."],
        }

    def system1(self, state: Dict[str, Any]) -> Dict[str, Any]:
        alert: Alert = state["alert"]
        forbidden = self.memory.forbidden_actions_for(alert.service)
        skill = self.skill_bank.match_skill_for_alert(alert, forbidden)
        if not skill:
            note = " (learned invariants vetoed a matching skill)" if forbidden and self.skill_bank.match_skill_for_alert(alert) else ""
            return {"cognition_mode": "system2", "steps": state["steps"] + [f"System 1: no skill matched{note}; escalating to diagnosis."]}
        target = skill.target_service or alert.service
        condition = next(c for c in skill.conditions if c.matches(alert.metric, alert.value))
        action = RemediationAction(
            action_id=f"act-{uuid.uuid4().hex[:8]}",
            action_type=skill.action_type,
            target_service=target,
            parameters=skill.parameters,
            rationale=f"Skill '{skill.name}': {skill.description}",
            rollback_plan=f"Reverse the {skill.action_type.value} on {target} if {alert.metric} has not recovered within 10 minutes.",
            skill_name=skill.name,
        )
        return {
            "cognition_mode": "system1",
            "proposed_action": action,
            "root_cause_hypothesis": f"Known signature: {alert.metric} {condition.comparator} {condition.threshold} matches skill '{skill.name}'.",
            "steps": state["steps"] + [f"System 1: matched skill '{skill.name}' ({skill.created_by})."],
        }

    def system2(self, state: Dict[str, Any]) -> Dict[str, Any]:
        alert: Alert = state["alert"]
        invariants = self.memory.invariants_for(alert.service)
        episodes = self.memory.similar_episodes(alert)
        result = diagnose(alert, self.topology, invariants, episodes, self.memory.forbidden_actions_for(alert.service))
        return {
            "cognition_mode": result.mode,
            "proposed_action": result.action,
            "root_cause_hypothesis": result.hypothesis,
            "evidence": result.evidence,
            "confidence": result.confidence,
            "diagnosis_note": result.note,
            "steps": state["steps"] + [
                f"System 2: {len(invariants)} rule(s) and {len(episodes)} similar incident(s) in context. {result.note}"
            ],
        }

    def guard(self, state: Dict[str, Any]) -> Dict[str, Any]:
        action: RemediationAction = state["proposed_action"]
        forbidden = self.memory.forbidden_actions_for(action.target_service)
        # Only verified skills may run without review; LLM and fallback proposals always go to an SRE.
        radius = BlastRadiusGuard.evaluate(action, self.topology, forbidden, novel=state["cognition_mode"] != "system1")
        return {
            "blast_radius": radius,
            "steps": state["steps"] + [
                f"Guard: risk {radius.risk_level.value}, {radius.user_traffic_pct}% of traffic, "
                f"{'SRE approval required' if radius.requires_sre_approval else 'within automatic limits'}."
            ],
        }

    def sre_gate(self, state: Dict[str, Any]) -> Dict[str, Any]:
        if not state["blast_radius"].requires_sre_approval:
            return {"decision": "auto"}
        response = interrupt({
            "incident_id": state["incident_id"],
            "proposed_action": state["proposed_action"].model_dump(mode="json"),
            "blast_radius": state["blast_radius"].model_dump(mode="json"),
            "invariants": [i.model_dump(mode="json") for i in self.memory.invariants_for(state["alert"].service)],
        })
        # The API validates the response; the override action is re-validated here all the same.
        override = response.get("override")
        if override:
            override = RemediationAction(**override)
            override.parameters = actions.validate_action(override.action_type, override.target_service, override.parameters, self.topology)
        return {"decision": response["decision"], "override": override, "sre_notes": response.get("notes")}

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        decision = state["decision"]
        if decision == "reject":
            return {"status": IncidentStatus.REJECTED.value, "execution_result": "Rejected by SRE; nothing was executed.",
                    "steps": state["steps"] + ["SRE rejected the proposal."]}
        action: RemediationAction = state["override"] if decision == "override" else state["proposed_action"]
        log = actions.simulate(action.action_type, action.target_service, action.parameters, self.topology)
        if decision != "override" and action.skill_name:
            self.skill_bank.record_success(action.skill_name)
        status = IncidentStatus.OVERRIDDEN if decision == "override" else IncidentStatus.RESOLVED
        who = {"auto": "Executed automatically", "approve": "Approved by SRE", "override": "SRE override executed"}[decision]
        return {"status": status.value, "execution_result": log, "steps": state["steps"] + [f"{who}: {log}"]}

    def learn(self, state: Dict[str, Any]) -> Dict[str, Any]:
        update: Dict[str, Any] = {}
        if state["decision"] in ("override", "reject"):
            trace, skill, invariant = self.reflexion.learn(
                incident_id=state["incident_id"],
                alert=state["alert"],
                proposed=state["proposed_action"],
                hypothesis=state.get("root_cause_hypothesis", ""),
                notes=state.get("sre_notes"),
                override=state.get("override"),
            )
            update = {
                "reflexion": trace,
                "learned_skill": skill,
                "steps": state["steps"] + [
                    "Reflexion: recorded invariant"
                    + (f" forbidding {', '.join(a.value for a in trace.forbidden_actions)}" if trace.forbidden_actions else "")
                    + (f" and learned skill '{skill.name}'." if skill else ".")
                ],
            }
        self.memory.record_episode({
            "incident_id": state["incident_id"],
            "alert": state["alert"].model_dump(mode="json"),
            "cognition_mode": state["cognition_mode"],
            "proposed_action": state["proposed_action"].model_dump(mode="json"),
            "outcome": state["status"],
            "sre_notes": state.get("sre_notes"),
        })
        return update

    # ---------------------------------------------------------------- graph

    def _build(self):
        g = StateGraph(IncidentGraphState)
        for name in ("ingest", "system1", "system2", "guard", "sre_gate", "execute", "learn"):
            g.add_node(name, getattr(self, name))
        g.set_entry_point("ingest")
        g.add_edge("ingest", "system1")
        g.add_conditional_edges("system1", lambda s: "guard" if s["cognition_mode"] == "system1" else "system2",
                                {"guard": "guard", "system2": "system2"})
        g.add_edge("system2", "guard")
        g.add_edge("guard", "sre_gate")
        g.add_edge("sre_gate", "execute")
        g.add_edge("execute", "learn")
        g.add_edge("learn", END)
        # ponytail: in-memory checkpoints; paused incidents are lost on restart. Use a Postgres checkpointer to keep them.
        return g.compile(checkpointer=MemorySaver())
