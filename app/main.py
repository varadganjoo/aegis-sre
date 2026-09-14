"""FastAPI Application for Aegis-SRE Autonomous Incident Response Platform.
Serves REST APIs for incident triage, LangGraph interrupt resumption, and the dark-mode SRE Command Center.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from langgraph.types import Command

from app.blast_radius import BlastRadiusGuard
from app.graph import (
    aegis_graph,
    default_topology,
    memory_kernel,
    reflexion_engine,
    skill_bank,
)
from app.schemas import (
    Alert,
    AlertSeverity,
    ClusterTopology,
    IncidentState,
    IncidentStatus,
    RemediationAction,
    RiskLevel,
)

app = FastAPI(
    title="Aegis-SRE Autonomous Incident Response Platform",
    description="Self-Learning SRE Agent using Dual-Process Cognition, Dynamic Skill Synthesis, and LangGraph HITL Gates.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# In-memory incident store
INCIDENTS_DB: Dict[str, Dict[str, Any]] = {}

# Demo Incidents
DEMO_INCIDENTS = [
    {
        "incident_id": "INC-8491",
        "title": "Payment Orchestrator 504 Gateway Timeouts",
        "alert": {
            "alert_id": "ALT-101",
            "service": "payment-orchestrator",
            "metric": "p99_latency_ms",
            "value": 1450.0,
            "threshold": 300.0,
            "severity": "critical",
            "description": "P99 latency surged to 1.45s across payment pods; upstream checkout-service experiencing cascading timeouts.",
        },
    },
    {
        "incident_id": "INC-8492",
        "title": "Postgres Primary Replication Lag & Connection Starvation",
        "alert": {
            "alert_id": "ALT-102",
            "service": "postgres-primary",
            "metric": "connection_pool_usage_pct",
            "value": 98.4,
            "threshold": 80.0,
            "severity": "critical",
            "description": "Stateful database connection pool exhausted (98.4%). Direct restart strictly blocked by institutional invariant.",
        },
    },
    {
        "incident_id": "INC-8493",
        "title": "API Gateway High CPU Spike (Known Signature)",
        "alert": {
            "alert_id": "ALT-103",
            "service": "api-gateway",
            "metric": "cpu_utilization_pct",
            "value": 89.2,
            "threshold": 75.0,
            "severity": "high",
            "description": "Stateless ingress CPU utilization spike due to seasonal traffic surge. Matches pre-learned HPA skill.",
        },
    },
]


class ResumeRequest(BaseModel):
    approved: bool = True
    override_notes: Optional[str] = None
    override_action: Optional[str] = None


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "platform": "Aegis-SRE Autonomous Incident Response",
        "skills_count": len(skill_bank.list_skills()),
        "invariants_count": len(memory_kernel.invariants),
    }


@app.get("/api/incidents")
def list_incidents():
    """Lists all active and demo incidents."""
    return DEMO_INCIDENTS


@app.get("/api/topology")
def get_topology():
    """Returns cluster topology DAG."""
    return default_topology.model_dump()


@app.get("/api/skills")
def list_skills():
    """Returns all procedural skills in the dynamic SkillBank."""
    return [s.model_dump() for s in skill_bank.list_skills()]


@app.get("/api/memory/invariants")
def list_invariants():
    """Returns learned institutional invariants."""
    return memory_kernel.invariants


@app.get("/api/memory/episodes")
def list_episodes():
    """Returns past incident episodes."""
    return memory_kernel.episodes


@app.post("/api/incidents/{incident_id}/diagnose")
def run_diagnosis(incident_id: str):
    """Executes the LangGraph incident triage and diagnosis workflow."""
    demo = next((inc for inc in DEMO_INCIDENTS if inc["incident_id"] == incident_id), None)
    if not demo:
        raise HTTPException(status_code=404, detail="Incident not found.")

    alert = Alert(**demo["alert"])
    config = {"configurable": {"thread_id": incident_id}}

    initial_state = {
        "incident_id": incident_id,
        "alert": alert,
        "title": demo["title"],
    }

    try:
        # Run graph until completion or interrupt()
        result = aegis_graph.invoke(initial_state, config=config)
        INCIDENTS_DB[incident_id] = result
        return {
            "incident_id": incident_id,
            "status": result.get("status"),
            "cognition_mode": result.get("cognition_mode"),
            "root_cause_hypothesis": result.get("root_cause_hypothesis"),
            "investigation_steps": result.get("investigation_steps", []),
            "proposed_action": result["proposed_action"].model_dump() if result.get("proposed_action") else None,
            "blast_radius": result["blast_radius"].model_dump() if result.get("blast_radius") else None,
            "requires_sre_approval": result.get("blast_radius", {}).requires_sre_approval if hasattr(result.get("blast_radius"), "requires_sre_approval") else False,
            "execution_result": result.get("execution_result"),
            "reflexion": result["reflexion"].model_dump() if result.get("reflexion") else None,
        }
    except Exception as e:
        # Check if paused at interrupt
        state_snap = aegis_graph.get_state(config)
        if state_snap and state_snap.tasks:
            for task in state_snap.tasks:
                if task.interrupts:
                    int_val = task.interrupts[0].value
                    return {
                        "incident_id": incident_id,
                        "status": "awaiting_sre_approval",
                        "cognition_mode": state_snap.values.get("cognition_mode"),
                        "root_cause_hypothesis": state_snap.values.get("root_cause_hypothesis"),
                        "investigation_steps": state_snap.values.get("investigation_steps", []),
                        "proposed_action": state_snap.values.get("proposed_action").model_dump() if state_snap.values.get("proposed_action") else None,
                        "blast_radius": state_snap.values.get("blast_radius").model_dump() if state_snap.values.get("blast_radius") else None,
                        "requires_sre_approval": True,
                        "interrupt_details": int_val,
                    }
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/incidents/{incident_id}/resume")
def resume_incident(incident_id: str, body: ResumeRequest):
    """Resumes an interrupted incident via LangGraph Command(resume=...)."""
    config = {"configurable": {"thread_id": incident_id}}

    resume_payload = {
        "approved": body.approved,
        "override_notes": body.override_notes,
        "override_action": body.override_action,
    }

    try:
        result = aegis_graph.invoke(Command(resume=resume_payload), config=config)
        INCIDENTS_DB[incident_id] = result
        return {
            "incident_id": incident_id,
            "status": result.get("status"),
            "cognition_mode": result.get("cognition_mode"),
            "execution_result": result.get("execution_result"),
            "investigation_steps": result.get("investigation_steps", []),
            "reflexion": result["reflexion"].model_dump() if result.get("reflexion") else None,
            "synthesized_skill": result["synthesized_skill"].model_dump() if result.get("synthesized_skill") else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resume incident: {e}")


# Serve UI
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")
