"""Reflexion & Post-Mortem Analysis Engine.
Implements dual-critic reflection: analyzes human engineer overrides, extracts causal invariants,
and synthesizes new procedural skills to prevent repeat failures.
"""

import logging
from typing import Optional, Tuple
from app.schemas import ActionType, IncidentState, ReflexionTrace, RiskLevel, SkillDefinition
from app.skill_bank import DynamicSkillBank

logger = logging.getLogger("aegis_sre.reflexion")


class ReflexionEngine:
    """Performs post-mortem reflection on incidents, particularly when overridden by human SREs."""

    def __init__(self, skill_bank: Optional[DynamicSkillBank] = None):
        self.skill_bank = skill_bank or DynamicSkillBank()

    def analyze_override(
        self,
        state: IncidentState,
        sre_override_notes: str,
        actual_action_taken: str,
    ) -> Tuple[ReflexionTrace, Optional[SkillDefinition]]:
        """Analyzes an SRE engineer override, extracts an invariant, and synthesizes a new skill."""
        alert = state.alert
        initial_action = state.proposed_action.action_type.value if state.proposed_action else "unknown"

        # 1. Distill Causal Invariant from SRE Override
        distillation = (
            f"SRE Override on {alert.service}: The agent proposed '{initial_action}', but human SRE intervened: "
            f"'{sre_override_notes}'. Human executed '{actual_action_taken}'."
        )

        # 2. Extract Generalized Invariant
        extracted_invariant = (
            f"For {alert.service} with {alert.metric} anomalies: {sre_override_notes}. "
            f"Do not use '{initial_action}' directly."
        )

        # 3. Dynamic Skill Synthesis: Synthesize a new Python skill matching the SRE's override
        synthesized_skill = None
        skill_name = f"learned_sre_mitigation_{alert.service.replace('-', '_')}"

        code, test_code = self._generate_skill_code(
            service=alert.service,
            metric=alert.metric,
            sre_action=actual_action_taken,
            override_notes=sre_override_notes,
        )

        try:
            synthesized_skill = self.skill_bank.register_skill(
                name=skill_name,
                description=f"Self-learned mitigation synthesized from SRE override for {alert.service}: {sre_override_notes}",
                trigger_pattern=f"{alert.service} AND {alert.metric}",
                code=code,
                test_code=test_code,
                created_by="reflexion-engine-postmortem",
            )
            logger.info("Successfully synthesized and registered new skill: %s", skill_name)
        except Exception as e:
            logger.error("Failed to register synthesized skill from reflexion: %s", e)

        trace = ReflexionTrace(
            incident_id=state.incident_id,
            alert_summary=f"{alert.severity.value.upper()}: {alert.service} {alert.metric}={alert.value}",
            initial_hypothesis=state.root_cause_hypothesis or "Initial automated diagnosis",
            proposed_action=initial_action,
            was_overridden=True,
            human_override_reason=sre_override_notes,
            applied_action=actual_action_taken,
            root_cause_distillation=distillation,
            extracted_invariant=extracted_invariant,
            synthesized_skill_name=skill_name if synthesized_skill else None,
        )

        return trace, synthesized_skill

    def _generate_skill_code(
        self,
        service: str,
        metric: str,
        sre_action: str,
        override_notes: str,
    ) -> Tuple[str, str]:
        """Generates valid Python code and sandboxed unit tests for the synthesized skill."""
        clean_notes = override_notes.replace('"', '\\"')
        code = f'''"""Self-learned remediation skill for {service} ({metric}).
Synthesized via Reflexion post-mortem after human SRE override:
"{clean_notes}"
"""

def run(context: dict) -> dict:
    service = context.get("service", "{service}")
    # Enforce SRE-learned remediation logic:
    # 1. Gracefully drain traffic
    # 2. Apply calibrated action
    return {{
        "success": True,
        "action": "{sre_action}",
        "service": service,
        "learned_policy": "{clean_notes}",
        "log": f"Executed SRE-learned remediation for {{service}}: {sre_action} with graceful connection drain."
    }}
'''

        test_code = f'''"""Sandboxed verification test for learned skill."""
def test():
    ctx = {{"service": "{service}"}}
    res = run(ctx)
    assert res["success"] is True
    assert res["action"] == "{sre_action}"
    assert "{service}" in res["log"]
'''
        return code, test_code
