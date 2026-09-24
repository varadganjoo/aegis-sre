"""FastAPI app for Aegis-SRE: demo incidents, diagnosis runs, SRE decisions, and the operations console UI."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from langgraph.types import Command

from app import actions
from app.graph import AegisRuntime
from app.llm import GROQ_MODEL, MODEL_CHAIN, _groq_configured, get_gemini_client
from app.schemas import ActionType

logger = logging.getLogger("aegis_sre.api")

app = FastAPI(
    title="Aegis-SRE",
    description="Incident diagnosis with an LLM, deterministic blast-radius guards, SRE sign-off, and skills learned from overrides.",
    version="2.0.0",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
runtime = AegisRuntime()

DEMO_INCIDENTS = [
    {
        "incident_id": "INC-8491",
        "title": "Payment orchestrator latency spike",
        "alert": {
            "alert_id": "ALT-101",
            "service": "payment-orchestrator",
            "metric": "p99_latency_ms",
            "value": 1450.0,
            "threshold": 300.0,
            "severity": "critical",
            "description": "P99 latency at 1.45s on payment pods; checkout-service is seeing cascading timeouts.",
        },
    },
    {
        "incident_id": "INC-8492",
        "title": "Postgres primary connection pool exhausted",
        "alert": {
            "alert_id": "ALT-102",
            "service": "postgres-primary",
            "metric": "connection_pool_usage_pct",
            "value": 98.4,
            "threshold": 80.0,
            "severity": "critical",
            "description": "Connection pool at 98.4%; new connections from auth, payment and inventory are queueing.",
        },
    },
    {
        "incident_id": "INC-8493",
        "title": "API gateway CPU spike",
        "alert": {
            "alert_id": "ALT-103",
            "service": "api-gateway",
            "metric": "cpu_utilization_pct",
            "value": 89.2,
            "threshold": 75.0,
            "severity": "high",
            "description": "Gateway CPU at 89% during a traffic surge; latency still within SLO.",
        },
    },
]
INCIDENTS = {i["incident_id"]: i for i in DEMO_INCIDENTS}


class OverrideAction(BaseModel):
    action_type: ActionType
    target_service: str = Field(..., max_length=64)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    rollback_plan: str = Field("Reverse this action if the alert does not clear.", max_length=500)


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., max_length=80)
    decision: Literal["approve", "override", "reject"]
    override: Optional[OverrideAction] = None
    notes: Optional[str] = Field(None, max_length=1000)


def _summary(incident_id: str, thread_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    interrupts = state.get("__interrupt__") or ()
    keys = [
        "status", "cognition_mode", "root_cause_hypothesis", "evidence", "confidence", "diagnosis_note", "steps",
        "proposed_action", "blast_radius", "decision", "sre_notes", "execution_result", "reflexion", "learned_skill",
    ]
    return jsonable_encoder({
        "incident_id": incident_id,
        "thread_id": thread_id,
        **{k: state.get(k) for k in keys},
        "awaiting_approval": bool(interrupts),
    })


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "models": MODEL_CHAIN if get_gemini_client() else [],
        "backup": f"groq/{GROQ_MODEL}" if _groq_configured() else None,
        "skills": len(runtime.skill_bank.list_skills()),
        "invariants": len(runtime.memory.invariants),
    }


@app.get("/api/incidents")
def list_incidents():
    return DEMO_INCIDENTS


@app.get("/api/topology")
def get_topology():
    return runtime.topology.model_dump(mode="json")


@app.get("/api/actions")
def list_actions():
    """The action allowlist with parameter schemas, so the UI's override form matches server validation."""
    return [
        {
            "action_type": action_type.value,
            "stateful_only": action_type in actions.STATEFUL_ONLY,
            "parameters": model.model_json_schema().get("properties", {}),
        }
        for action_type, model in actions.PARAMS.items()
    ]


@app.get("/api/skills")
def list_skills():
    return [s.model_dump(mode="json") for s in runtime.skill_bank.list_skills()]


@app.get("/api/memory/invariants")
def list_invariants():
    return [i.model_dump(mode="json") for i in runtime.memory.invariants]


@app.get("/api/memory/episodes")
def list_episodes():
    return list(reversed(runtime.memory.episodes[-20:]))


@app.post("/api/incidents/{incident_id}/diagnose")
def run_diagnosis(incident_id: str):
    """Runs the workflow on a fresh thread. Pauses at the SRE gate when approval is required."""
    incident = INCIDENTS.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")
    thread_id = f"{incident_id}-{uuid.uuid4().hex[:10]}"
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = runtime.graph.invoke({"incident_id": incident_id, "alert": incident["alert"]}, config)
    except Exception as exc:
        logger.exception("Diagnosis run failed")
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {exc}") from exc
    return _summary(incident_id, thread_id, state)


@app.post("/api/incidents/{incident_id}/resume")
def resume_incident(incident_id: str, body: ResumeRequest):
    if incident_id not in INCIDENTS or not body.thread_id.startswith(f"{incident_id}-"):
        raise HTTPException(status_code=404, detail="Unknown incident or thread.")
    config = {"configurable": {"thread_id": body.thread_id}}
    snapshot = runtime.graph.get_state(config)
    if not any(task.interrupts for task in snapshot.tasks):
        raise HTTPException(
            status_code=409,
            detail="Nothing is waiting for a decision on this run (it was already decided, or the server restarted). Diagnose again.",
        )

    override = None
    if body.decision == "override":
        if body.override is None:
            raise HTTPException(status_code=422, detail="An override decision needs an override action.")
        try:
            parameters = actions.validate_action(
                body.override.action_type, body.override.target_service, body.override.parameters, runtime.topology
            )
        except actions.InvalidAction as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        override = {
            "action_id": f"override-{uuid.uuid4().hex[:8]}",
            "action_type": body.override.action_type.value,
            "target_service": body.override.target_service,
            "parameters": parameters,
            "rationale": "SRE override",
            "rollback_plan": body.override.rollback_plan,
        }

    state = runtime.graph.invoke(
        Command(resume={"decision": body.decision, "override": override, "notes": body.notes}), config
    )
    return _summary(incident_id, body.thread_id, state)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")
