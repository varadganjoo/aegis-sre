"""Dynamic Skill Bank & Code-as-Skill Acquisition Engine (Voyager/ExpeL architecture).
Enables the agent to synthesize, test in an AST sandbox, and dynamically register new executable Python skills.
"""

import ast
import json
import logging
import os
import subprocess
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

    @staticmethod
    def _get_sanitized_env() -> Dict[str, str]:
        """Returns a sanitized environment dictionary for isolated skill execution.
        Strips API keys, tokens, credentials, and sensitive secrets to prevent
        synthesized code from accessing parent credentials.
        """
        safe_keys = {
            "PATH",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
            "WINDIR",
            "PYTHONPATH",
            "PYTHONHOME",
        }
        sensitive_patterns = (
            "KEY",
            "TOKEN",
            "SECRET",
            "AUTH",
            "PASS",
            "CREDENTIAL",
            "GEMINI",
            "OPENAI",
            "ANTHROPIC",
            "AWS",
            "AZURE",
            "GITHUB",
            "GH_",
        )
        sanitized = {}
        for k, v in os.environ.items():
            k_upper = k.upper()
            if k_upper in safe_keys:
                sanitized[k] = v
            elif any(pat in k_upper for pat in sensitive_patterns):
                continue
            else:
                sanitized[k] = v
        return sanitized

    def run_sandbox_tests(self, code: str, test_code: str) -> bool:
        """Executes test_code against code in an isolated execution sandbox subprocess."""
        test_runner_script = f"""import sys

# Skill implementation
{code}

if 'run' not in globals() or not callable(globals()['run']):
    sys.exit(2)

# Skill unit test
{test_code}

if 'test' in globals() and callable(globals()['test']):
    globals()['test']()
"""
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", test_runner_script],
                capture_output=True,
                text=True,
                timeout=5,
                env=self._get_sanitized_env(),
            )
            if proc.returncode != 0:
                logger.error("Sandbox test failed (exit code %d): %s\n%s", proc.returncode, proc.stdout, proc.stderr)
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("Sandbox test execution timed out (5s limit).")
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
        """Executes a registered skill in an isolated subprocess with timeout."""
        skill = self.get_skill(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found in registry.")

        runner_script = f"""import sys
import json

{skill.code}

if 'run' not in globals() or not callable(globals()['run']):
    sys.stderr.write("Skill lacks callable 'run(context)' entrypoint.")
    sys.exit(2)

input_data = json.loads(sys.stdin.read())
result = globals()['run'](input_data)
sys.stdout.write(json.dumps(result))
"""
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", runner_script],
                input=json.dumps(context),
                capture_output=True,
                text=True,
                timeout=10,
                env=self._get_sanitized_env(),
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Skill execution failed (exit code {proc.returncode}): {proc.stderr}")

            result = json.loads(proc.stdout)
            skill.success_count += 1
            self._persist_registry()
            return result
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"Skill '{name}' execution timed out after 10 seconds.") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Skill '{name}' did not return valid JSON output: {proc.stdout}") from e

