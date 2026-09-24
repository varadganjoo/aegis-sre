"""Model Context Protocol server for Aegis-SRE.

Read-only resources (topology, skills, invariants) and tools that evaluate actions without executing them.
There is no tool that runs or registers code: actions come from the same allowlist the API enforces.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer

from app import actions
from app.blast_radius import BlastRadiusGuard
from app.graph import AegisRuntime
from app.schemas import ActionType, RemediationAction

mcp = MCPServer("aegis-sre-mcp")
runtime = AegisRuntime()


@mcp.resource("sre://topology")
def get_topology_resource() -> str:
    """The cluster topology as JSON."""
    return runtime.topology.model_dump_json(indent=2)


@mcp.resource("sre://skills")
def get_skills_resource() -> str:
    """Procedural skills (trigger conditions and allowlisted actions) as JSON."""
    return json.dumps([s.model_dump(mode="json") for s in runtime.skill_bank.list_skills()], indent=2)


@mcp.resource("sre://invariants")
def get_invariants_resource() -> str:
    """Institutional rules, including actions forbidden per service, as JSON."""
    return json.dumps([i.model_dump(mode="json") for i in runtime.memory.invariants], indent=2)


@mcp.tool()
def query_service_health(service_name: str) -> Dict[str, Any]:
    """Replicas, dependencies, and statefulness for one service."""
    node = runtime.topology.services.get(service_name)
    if not node:
        return {"error": f"Service '{service_name}' not found."}
    return node.model_dump(mode="json")


@mcp.tool()
def list_allowed_actions() -> list[Dict[str, Any]]:
    """The remediation allowlist and each action's parameter schema."""
    return [
        {"action_type": a.value, "stateful_only": a in actions.STATEFUL_ONLY, "parameters": m.model_json_schema().get("properties", {})}
        for a, m in actions.PARAMS.items()
    ]


@mcp.tool()
def calculate_blast_radius(
    target_service: str,
    action_type: str,
    rollback_plan: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validates a proposed action and returns its blast radius. Nothing is executed."""
    try:
        kind = ActionType(action_type)
        params = actions.validate_action(kind, target_service, parameters or {}, runtime.topology)
    except (ValueError, actions.InvalidAction) as exc:
        return {"error": str(exc)}
    action = RemediationAction(
        action_id="mcp-eval", action_type=kind, target_service=target_service, parameters=params,
        rationale="MCP blast-radius evaluation", rollback_plan=rollback_plan,
    )
    forbidden = runtime.memory.forbidden_actions_for(target_service)
    return BlastRadiusGuard.evaluate(action, runtime.topology, forbidden).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run()
