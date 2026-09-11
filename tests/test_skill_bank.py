"""Unit tests for Dynamic Skill Bank & AST Sandbox Security."""

import pytest
from app.schemas import Alert, AlertSeverity
from app.skill_bank import DynamicSkillBank


@pytest.fixture
def skill_bank(tmp_path):
    return DynamicSkillBank(skills_dir=tmp_path / "skills")


def test_builtin_skills_seeded(skill_bank):
    """Verifies default SRE skills are seeded and available."""
    skills = skill_bank.list_skills()
    assert len(skills) >= 2
    names = [s.name for s in skills]
    assert "horizontal_pod_scaler" in names
    assert "circuit_breaker_tripper" in names


def test_ast_blocks_os_system(skill_bank):
    """AST Security Invariant: Blocks malicious os.system calls."""
    malicious_code = """
import os
def run(ctx):
    os.system("rm -rf /")
    return {"status": "pwned"}
"""
    violations = skill_bank.validate_code_ast(malicious_code)
    assert any("os.system()" in v for v in violations)


def test_ast_blocks_eval_and_exec(skill_bank):
    """AST Security Invariant: Blocks eval() and exec() within synthesized skills."""
    malicious_code = """
def run(ctx):
    eval("2 + 2")
    return {"status": "ok"}
"""
    violations = skill_bank.validate_code_ast(malicious_code)
    assert any("eval()" in v for v in violations)


def test_sandboxed_test_runner_passes_valid_code(skill_bank):
    """Verifies that correctly typed code and tests pass the sandbox."""
    code = """
def run(ctx):
    return {"success": True, "count": ctx.get("val", 0) + 1}
"""
    test_code = """
def test():
    res = run({"val": 5})
    assert res["success"] is True
    assert res["count"] == 6
"""
    passed = skill_bank.run_sandbox_tests(code, test_code)
    assert passed is True


def test_sandboxed_test_runner_catches_assertion_error(skill_bank):
    """Verifies that failing test assertions cause sandbox rejection."""
    code = """
def run(ctx):
    return {"success": False}
"""
    test_code = """
def test():
    res = run({})
    assert res["success"] is True  # Will fail!
"""
    passed = skill_bank.run_sandbox_tests(code, test_code)
    assert passed is False


def test_dynamic_registration_and_execution(skill_bank):
    """Verifies end-to-end skill synthesis, registration, and runtime execution."""
    code = """
def run(ctx):
    svc = ctx.get("service")
    return {"success": True, "log": f"Drained connections for {svc}."}
"""
    test_code = """
def test():
    res = run({"service": "test-svc"})
    assert res["success"] is True
"""
    skill = skill_bank.register_skill(
        name="test_drain_skill",
        description="Test skill for draining connections",
        trigger_pattern="test-svc AND drain",
        code=code,
        test_code=test_code,
    )
    assert skill.name == "test_drain_skill"

    # Execute it
    out = skill_bank.execute_skill("test_drain_skill", {"service": "test-svc"})
    assert out["success"] is True
    assert "Drained connections for test-svc" in out["log"]
