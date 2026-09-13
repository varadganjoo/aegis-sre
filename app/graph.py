"""LangGraph StateGraph for Aegis-SRE Autonomous Incident Response Platform.
Implements Dual-Process Cognition (System 1 vs System 2), Dynamic Skill Synthesis,
Blast-Radius Safety Guardrails, and LangGraph interrupt() Human-in-the-Loop gates.
"""

from typing import Any, Dict, Optional
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from app.blast_radius import BlastRadiusGuard
from app.memory import ExperientialMemoryKernel
from app.reflexion import ReflexionEngine
from app.schemas import (
    ActionType,
    Alert,
    ClusterTopology,
    IncidentState,
    IncidentStatus,
    RemediationAction,
    RiskLevel,
    ServiceNode,
    ServiceStatus,
    ServiceType,
)
from app.skill_bank import DynamicSkillBank


def get_default_topology() -> ClusterTopology:
    """Returns a realistic enterprise microservices cluster topology."""
    services = {
        "api-gateway": ServiceNode(
            name="api-gateway",
            type=ServiceType.GATEWAY,
            replicas=4,
            ready_replicas=4,
            dependencies=["auth-service", "checkout-service", "inventory-service"],
        ),
        "auth-service": ServiceNode(
            name="auth-service",
            type=ServiceType.SERVICE,
            replicas=3,
            ready_replicas=3,
            dependencies=["redis-cluster", "postgres-primary"],
        ),
        "checkout-service": ServiceNode(
            name="checkout-service",
            type=ServiceType.SERVICE,
            replicas=4,
            ready_replicas=4,
            dependencies=["payment-orchestrator", "redis-cluster", "inventory-service"],
        ),
        "payment-orchestrator": ServiceNode(
            name="payment-orchestrator",
            type=ServiceType.SERVICE,
            replicas=2,
            ready_replicas=2,
            dependencies=["postgres-primary"],
        ),
        "inventory-service": ServiceNode(
            name="inventory-service",
            type=ServiceType.SERVICE,
            replicas=3,
            ready_replicas=3,
            dependencies=["postgres-primary"],
        ),
        "redis-cluster": ServiceNode(
            name="redis-cluster",
            type=ServiceType.CACHE,
            replicas=3,
            ready_replicas=3,
            is_stateful=True,
        ),
        "postgres-primary": ServiceNode(
            name="postgres-primary",
            type=ServiceType.DATABASE,
            replicas=1,
            ready_replicas=1,
            is_stateful=True,
        ),
    }
    return ClusterTopology(services=services)


# Initialize components
skill_bank = DynamicSkillBank()
memory_kernel = ExperientialMemoryKernel()
reflexion_engine = ReflexionEngine(skill_bank)
default_topology = get_default_topology()


# --- LangGraph Node Implementations ---

def ingest_telemetry_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Ingests alert and initializes incident state."""
    raw_alert = state.get("alert")
    if isinstance(raw_alert, dict):
        alert = Alert(**raw_alert)
    else:
        alert = raw_alert

    state["alert"] = alert
    state["title"] = f"Incident: {alert.severity.value.upper()} on {alert.service} ({alert.metric})"
    state["investigation_steps"] = [f"Ingested alert {alert.alert_id} for service '{alert.service}'."]
    state["status"] = IncidentStatus.INVESTIGATING.value
    return state


def system1_triage_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """System 1: Checks for pre-learned procedural skills in SkillBank (Fast Path)."""
    alert: Alert = state["alert"]
    matched_skill = skill_bank.match_skill_for_alert(alert)

    # Check if there are learned invariants that forbid fast-path
    invariants = memory_kernel.query_invariants_for_service(alert.service)

    if matched_skill and not any("Never" in inv for inv in invariants):
        state["cognition_mode"] = "system1_fast_path"
        state["investigation_steps"].append(
            f"System 1 Match: Identified verified procedural skill '{matched_skill.name}'. Bypassing deliberate LLM reasoning."
        )
        state["root_cause_hypothesis"] = f"Known metric signature matching skill '{matched_skill.name}'."
        state["proposed_action"] = RemediationAction(
            action_id=f"act-{alert.alert_id}",
            action_type=ActionType.CUSTOM_SKILL,
            target_service=alert.service,
            parameters={"skill_name": matched_skill.name},
            risk_level=RiskLevel.LOW,
            rationale=f"System 1 fast-path execution of verified skill '{matched_skill.name}'.",
            rollback_plan=f"Revert state changes applied by {matched_skill.name}.",
            skill_name=matched_skill.name,
        )
    else:
        state["cognition_mode"] = "system2_deliberate"
        state["investigation_steps"].append(
            "System 1 Miss: No pre-learned skill matched or institutional invariant requires deliberate analysis. Activating System 2 Causal Reasoner."
        )

    return state


def route_cognition_mode(state: Dict[str, Any]) -> str:
    """Routes to either System 1 fast execution or System 2 deliberate reasoning."""
    if state.get("cognition_mode") == "system1_fast_path":
        return "blast_radius_guard"
    return "system2_diagnose"


def system2_diagnose_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """System 2: Deep causal reasoning, invariant retrieval, and novel remediation formulation."""
    alert: Alert = state["alert"]
    invariants = memory_kernel.query_invariants_for_service(alert.service)
    similar_episodes = memory_kernel.query_similar_episodes(alert)

    state["investigation_steps"].append(
        f"System 2 Reasoning: Retrieved {len(invariants)} institutional invariants and {len(similar_episodes)} historical episodes."
    )

    # Formulate hypothesis and remediation based on topology & metrics
    target_node = default_topology.services.get(alert.service)
    is_stateful = target_node.is_stateful if target_node else False

    if is_stateful:
        state["root_cause_hypothesis"] = (
            f"Stateful infrastructure degradation on {alert.service}. "
            f"Direct restart is strictly forbidden by institutional invariant. Requires graceful failover or pool drain."
        )
        action = RemediationAction(
            action_id=f"act-{alert.alert_id}",
            action_type=ActionType.DRAIN,
            target_service=alert.service,
            parameters={"drain_timeout_seconds": 60, "graceful": True},
            risk_level=RiskLevel.CRITICAL,
            rationale=f"Graceful drain and connection shed for stateful {alert.service} to prevent split-brain.",
            rollback_plan="Re-enable ingress traffic routing and restore connection pool limits.",
        )
    else:
        state["root_cause_hypothesis"] = (
            f"Cascading upstream load or memory leak on {alert.service} causing {alert.metric} spike to {alert.value}."
        )
        action = RemediationAction(
            action_id=f"act-{alert.alert_id}",
            action_type=ActionType.SCALE,
            target_service=alert.service,
            parameters={"target_replicas": 6},
            risk_level=RiskLevel.MEDIUM,
            rationale=f"Scale {alert.service} from {target_node.replicas if target_node else 2} to 6 replicas to absorb traffic surge.",
            rollback_plan=f"Scale back {alert.service} to baseline {target_node.replicas if target_node else 2} replicas once latency drops below threshold.",
        )

    state["proposed_action"] = action
    return state


def blast_radius_guard_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calculates blast radius and enforces deterministic safety invariants."""
    action: RemediationAction = state["proposed_action"]
    blast_radius = BlastRadiusGuard.evaluate(action, default_topology)
    state["blast_radius"] = blast_radius
    state["investigation_steps"].append(
        f"Blast Radius Evaluated: Risk={blast_radius.risk_level.value.upper()}, Impacted Services={blast_radius.impacted_services}, Traffic={blast_radius.user_traffic_pct}%."
    )
    return state


def sre_review_gate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph interrupt() gate: Pauses execution if action requires SRE authorization."""
    blast_radius: BlastRadius = state["blast_radius"]

    if blast_radius.requires_sre_approval:
        state["status"] = IncidentStatus.AWAITING_APPROVAL.value
        state["investigation_steps"].append(
            f"HITL Gate: Paused at SRE review gate. Reasons: {'; '.join(blast_radius.reasons)}"
        )

        # Interrupt the graph and yield control to the SRE
        resume_data = interrupt({
            "incident_id": state.get("incident_id"),
            "proposed_action": state["proposed_action"].model_dump() if state.get("proposed_action") else None,
            "blast_radius": blast_radius.model_dump(),
            "invariants": memory_kernel.query_invariants_for_service(state["alert"].service),
        })

        # When resumed via Command(resume=...)
        state["sre_approved"] = resume_data.get("approved", True)
        state["sre_override_notes"] = resume_data.get("override_notes")
        state["sre_override_action"] = resume_data.get("override_action")
    else:
        state["sre_approved"] = True

    return state


def execute_remediation_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Executes the approved remediation or records an SRE override."""
    is_approved = state.get("sre_approved", True)
    override_notes = state.get("sre_override_notes")
    override_action = state.get("sre_override_action")

    if not is_approved or override_notes:
        state["status"] = IncidentStatus.OVERRIDDEN.value
        action_name = override_action or "manual_sre_intervention"
        state["execution_result"] = f"SRE Overrode proposal: {override_notes}. Executed: {action_name}"
        state["investigation_steps"].append(f"SRE Override Applied: {state['execution_result']}")
    else:
        state["status"] = IncidentStatus.RESOLVED.value
        action: RemediationAction = state["proposed_action"]
        if action.action_type == ActionType.CUSTOM_SKILL and action.skill_name:
            result = skill_bank.execute_skill(action.skill_name, {"service": action.target_service})
            state["execution_result"] = result.get("log", "Skill executed successfully.")
        else:
            state["execution_result"] = f"Successfully executed {action.action_type.value} on {action.target_service}."
        state["investigation_steps"].append(f"Remediation Executed: {state['execution_result']}")

    return state


def reflexion_learning_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reflexion: If overridden, extracts institutional invariants and synthesizes a new skill."""
    override_notes = state.get("sre_override_notes")
    override_action = state.get("sre_override_action", "manual_sre_intervention")

    # Build IncidentState object for memory recording
    incident_state = IncidentState(
        incident_id=state.get("incident_id", "inc-001"),
        title=state.get("title", "Incident"),
        alert=state["alert"],
        status=IncidentStatus(state["status"]),
        cognition_mode=state.get("cognition_mode", "system2_deliberate"),
        root_cause_hypothesis=state.get("root_cause_hypothesis"),
        investigation_steps=state.get("investigation_steps", []),
        proposed_action=state.get("proposed_action"),
        blast_radius=state.get("blast_radius"),
        sre_approved=state.get("sre_approved"),
        sre_override_notes=override_notes,
        execution_result=state.get("execution_result"),
    )

    if override_notes:
        trace, new_skill = reflexion_engine.analyze_override(
            state=incident_state,
            sre_override_notes=override_notes,
            actual_action_taken=override_action,
        )
        incident_state.reflexion = trace
        incident_state.synthesized_skill = new_skill
        state["reflexion"] = trace
        state["synthesized_skill"] = new_skill
        state["investigation_steps"].append(
            f"Reflexion Complete: Synthesized new skill '{trace.synthesized_skill_name}' and extracted invariant: '{trace.extracted_invariant}'."
        )

    # Record episode in permanent memory kernel
    memory_kernel.record_episode(incident_state)
    return state


# --- Build LangGraph StateGraph ---

def build_aegis_graph() -> Any:
    workflow = StateGraph(dict)

    workflow.add_node("ingest_telemetry", ingest_telemetry_node)
    workflow.add_node("system1_triage", system1_triage_node)
    workflow.add_node("system2_diagnose", system2_diagnose_node)
    workflow.add_node("blast_radius_guard", blast_radius_guard_node)
    workflow.add_node("sre_review_gate", sre_review_gate_node)
    workflow.add_node("execute_remediation", execute_remediation_node)
    workflow.add_node("reflexion_learning", reflexion_learning_node)

    workflow.set_entry_point("ingest_telemetry")

    workflow.add_edge("ingest_telemetry", "system1_triage")

    workflow.add_conditional_edges(
        "system1_triage",
        route_cognition_mode,
        {
            "blast_radius_guard": "blast_radius_guard",
            "system2_diagnose": "system2_diagnose",
        },
    )

    workflow.add_edge("system2_diagnose", "blast_radius_guard")
    workflow.add_edge("blast_radius_guard", "sre_review_gate")
    workflow.add_edge("sre_review_gate", "execute_remediation")
    workflow.add_edge("execute_remediation", "reflexion_learning")
    workflow.add_edge("reflexion_learning", END)

    # Compile with durable memory saver
    checkpointer = MemorySaver()
    return workflow.compile(checkpointer=checkpointer)


# Compiled graph instance
aegis_graph = build_aegis_graph()
