"""Dynamic Skill Bank & Code-as-Skill Acquisition Engine (Voyager/ExpeL architecture).
Enables the agent to synthesize, test in an AST sandbox, and dynamically register new executable Python skills.
"""

import ast
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.schemas import Alert, SkillDefinition

logger = logging.getLogger("aegis_sre.skill_bank")

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
REGISTRY_FILE = SKILLS_DIR / "skills_registry.json"


class ASTSecurityVisitor(ast.NodeVisitor):
    """Inspects Python Abstract Syntax Trees for forbidden or dangerous system calls."""

    FORBIDDEN_CALLS = {
        "os.system",
        "os.remove",
        "os.unlink",
        "shutil.rmtree",
        "subprocess.call",
        "subprocess.Popen",
        "eval",
        "exec",
        "__import__",
    }

    FORBIDDEN_MODULES = {"pty", "socketserver", "ftplib", "smtplib"}

    def __init__(self):
        self.violations: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.name in self.FORBIDDEN_MODULES:
                self.violations.append(f"Forbidden module imported: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module in self.FORBIDDEN_MODULES:
            self.violations.append(f"Forbidden module imported: {node.module}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        call_name = ""
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"

        if call_name in self.FORBIDDEN_CALLS:
            self.violations.append(f"Forbidden dangerous call: {call_name}()")
        self.generic_visit(node)


class DynamicSkillBank:
    """Manages the lifecycle of synthesized procedural skills."""

    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = skills_dir or SKILLS_DIR
        self.registry_path = self.skills_dir / "skills_registry.json"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: Dict[str, SkillDefinition] = {}
        self._load_registry()

    def _load_registry(self):
        """Loads registered skills from disk."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("skills", []):
                        skill = SkillDefinition(**item)
                        self._skills[skill.name] = skill
            except Exception as e:
                logger.error("Failed to load skill registry: %s", e)

        # Seed initial skills if empty
        if not self._skills:
            self._seed_builtin_skills()

    def _seed_builtin_skills(self):
        """Seeds foundational production SRE skills."""
        hpa_code = '''"""Horizontal Pod Autoscaler Skill."""
def run(context: dict) -> dict:
    service = context.get("service")
    current = context.get("replicas", 2)
    target = min(current * 2, 10)
    return {
        "success": True,
        "action": "scale_replicas",
        "service": service,
        "previous_replicas": current,
        "target_replicas": target,
        "log": f"Scaled {service} from {current} to {target} pods under high traffic load."
    }
'''
        hpa_test = '''"""Test for HPA skill."""
def test():
    ctx = {"service": "checkout-service", "replicas": 3}
    res = run(ctx)
    assert res["success"] is True
    assert res["target_replicas"] == 6
'''
        self.register_skill(
            name="horizontal_pod_scaler",
            description="Scales stateless deployments under CPU/memory/latency pressure.",
            trigger_pattern="cpu_utilization_pct > 80.0 OR p99_latency_ms > 200.0",
            code=hpa_code,
            test_code=hpa_test,
        )

        cb_code = '''"""Circuit Breaker Tripper Skill."""
def run(context: dict) -> dict:
    service = context.get("service")
    return {
        "success": True,
        "action": "trip_circuit_breaker",
        "service": service,
        "log": f"Tripped circuit breaker for {service} to prevent downstream cascading failure."
    }
'''
        cb_test = '''"""Test for Circuit Breaker skill."""
def test():
    ctx = {"service": "payment-orchestrator"}
    res = run(ctx)
    assert res["success"] is True
    assert res["action"] == "trip_circuit_breaker"
'''
        self.register_skill(
            name="circuit_breaker_tripper",
            description="Isolates degraded downstream dependencies via circuit breaking.",
            trigger_pattern="error_rate_pct > 10.0",
            code=cb_code,
            test_code=cb_test,
        )

    def validate_code_ast(self, code: str) -> List[str]:
        """Validates Python syntax and checks for security violations via AST."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"Syntax error in synthesized code: {e}"]

        visitor = ASTSecurityVisitor()
        visitor.visit(tree)
        return visitor.violations

    def run_sandbox_tests(self, code: str, test_code: str) -> bool:
        """Executes test_code against code in an isolated execution sandbox."""
        sandbox_scope: Dict[str, Any] = {"__name__": "__sandbox__"}
        try:
            # 1. Execute the main skill code
            exec(code, sandbox_scope)
            if "run" not in sandbox_scope or not callable(sandbox_scope["run"]):
                logger.error("Synthesized skill lacks callable 'run(context)' entrypoint.")
                return False

            # 2. Execute test code
            exec(test_code, sandbox_scope)
            if "test" in sandbox_scope and callable(sandbox_scope["test"]):
                sandbox_scope["test"]()
            return True
        except AssertionError as e:
            logger.error("Sandbox test assertion failed: %s", e)
            return False
        except Exception as e:
            logger.error("Sandbox execution error: %s", e)
            return False

    def register_skill(
        self,
        name: str,
        description: str,
        trigger_pattern: str,
        code: str,
        test_code: str,
        created_by: str = "gemini-3.8-flash-synthesizer",
    ) -> SkillDefinition:
        """Validates, sandboxes, and registers a new procedural skill."""
        # 1. AST Security Inspection
        violations = self.validate_code_ast(code)
        if violations:
            raise ValueError(f"AST Security validation failed: {'; '.join(violations)}")

        # 2. Sandboxed Test Execution
        passed = self.run_sandbox_tests(code, test_code)
        if not passed:
            raise RuntimeError("Sandboxed unit tests failed for synthesized skill.")

        # 3. Save to disk
        skill_file = self.skills_dir / f"{name}.py"
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(code)

        # 4. Update memory & registry
        skill = SkillDefinition(
            skill_id=f"skill-{len(self._skills) + 1:03d}",
            name=name,
            description=description,
            trigger_pattern=trigger_pattern,
            code=code,
            test_code=test_code,
            version=1,
            created_by=created_by,
        )
        self._skills[name] = skill
        self._persist_registry()
        return skill

    def _persist_registry(self):
        """Persists the skill registry to JSON."""
        data = {"skills": [s.model_dump() for s in self._skills.values()]}
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def list_skills(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def match_skill_for_alert(self, alert: Alert) -> Optional[SkillDefinition]:
        """Matches incoming alert against procedural skill triggers (System 1 fast path)."""
        for skill in self._skills.values():
            pat = skill.trigger_pattern.lower()
            if alert.metric.lower() in pat or alert.severity.value.lower() in pat:
                return skill
        return None

    def execute_skill(self, name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a registered skill with the given context."""
        skill = self.get_skill(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found in registry.")

        scope: Dict[str, Any] = {}
        exec(skill.code, scope)
        result = scope["run"](context)
        skill.success_count += 1
        self._persist_registry()
        return result
