"""Model Context Protocol (MCP) Server for Aegis-SRE.
Exposes cluster topology resources, dynamic skills, and incident diagnostic tools over protocol.
"""

import json
from typing import Any, Dict
from mcp.server.mcpserver import MCPServer

from app.blast_radius import BlastRadiusGuard
from app.graph import default_topology, memory_kernel, skill_bank
from app.schemas import ActionType, Alert, AlertSeverity, RemediationAction, RiskLevel

# Initialize MCP server
mcp = MCPServer("aegis-sre-mcp")


# --- Resources ---

@mcp.resource("sre://topology")
def get_topology_resource() -> str:
    """Returns the live Kubernetes cluster topology as JSON."""
    return default_topology.model_dump_json(indent=2)


@mcp.resource("sre://skills")
def get_skills_resource() -> str:
    """Returns all registered procedural skills in the dynamic SkillBank."""
    skills = [s.model_dump() for s in skill_bank.list_skills()]
    return json.dumps(skills, indent=2)


@mcp.resource("sre://invariants")
def get_invariants_resource() -> str:
    """Returns institutional SRE invariants learned over time."""
    return json.dumps(memory_kernel.invariants, indent=2)


# --- Tools ---

@mcp.tool()
def query_service_health(service_name: str) -> Dict[str, Any]:
    """Queries health, metrics, and dependencies for a specific microservice."""
    node = default_topology.services.get(service_name)
    if not node:
        return {"error": f"Service '{service_name}' not found in cluster."}
    return node.model_dump()


@mcp.tool()
def calculate_blast_radius(
    target_service: str,
    action_type: str,
    rollback_plan: str,
) -> Dict[str, Any]:
    """Calculates the blast radius and safety risk for a proposed remediation action."""
    try:
        act_type = ActionType(action_type)
    except ValueError:
        act_type = ActionType.CUSTOM_SKILL

    action = RemediationAction(
        action_id="mcp-eval-01",
        action_type=act_type,
        target_service=target_service,
        rationale="MCP requested blast-radius evaluation",
        rollback_plan=rollback_plan,
    )
    result = BlastRadiusGuard.evaluate(action, default_topology)
    return result.model_dump()


@mcp.tool()
def execute_skill(skill_name: str, service: str) -> Dict[str, Any]:
    """Executes a verified procedural skill from the SkillBank."""
    try:
        return skill_bank.execute_skill(skill_name, {"service": service})
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def synthesize_procedural_skill(
    name: str,
    description: str,
    trigger_pattern: str,
    code: str,
    test_code: str,
) -> Dict[str, Any]:
    """Validates code via AST inspection, executes sandboxed tests, and registers a new skill."""
    try:
        skill = skill_bank.register_skill(
            name=name,
            description=description,
            trigger_pattern=trigger_pattern,
            code=code,
            test_code=test_code,
            created_by="mcp-client",
        )
        return {"success": True, "skill": skill.model_dump()}
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
