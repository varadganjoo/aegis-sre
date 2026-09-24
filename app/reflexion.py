"""Reflexion: learning from SRE overrides and rejections.

When an SRE overrides a proposal, Aegis records (1) an invariant forbidding the rejected action on that service,
so the guard flags it and System 1 skips it next time, and (2) a skill that repeats the SRE's chosen action when the
same alert condition recurs. Both are data. The SRE's notes are stored as text for people and as context for the
diagnosis model; they are never executed or compiled.
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.memory import ExperientialMemoryKernel
from app.schemas import Alert, Condition, Invariant, ReflexionTrace, RemediationAction, SkillDefinition
from app.skill_bank import DynamicSkillBank


class ReflexionEngine:
    def __init__(self, skill_bank: DynamicSkillBank, memory: ExperientialMemoryKernel):
        self.skill_bank = skill_bank
        self.memory = memory

    def learn(
        self,
        incident_id: str,
        alert: Alert,
        proposed: RemediationAction,
        hypothesis: str,
        notes: Optional[str],
        override: Optional[RemediationAction],
    ) -> Tuple[ReflexionTrace, Optional[SkillDefinition], Invariant]:
        """`override` is the action the SRE ran instead; None means the proposal was rejected outright."""
        forbidden = [proposed.action_type] if override_changes_type(proposed, override) else []
        rule = (notes or "").strip() or (
            f"SRE {'rejected' if override is None else 'replaced'} {proposed.action_type.value} on "
            f"{proposed.target_service} for {alert.metric} alerts."
        )
        invariant = self.memory.add_invariant(
            Invariant(service=proposed.target_service, rule=rule, forbidden_actions=forbidden, source=f"reflexion:{incident_id}")
        )

        skill = None
        if override is not None:
            skill = self.skill_bank.register_skill(
                name=f"learned_{alert.service}_{alert.metric}".replace("-", "_"),
                description=f"Learned from SRE override on {incident_id}: {override.action_type.value} on {override.target_service}.",
                services=[alert.service],
                conditions=[Condition(metric=alert.metric, comparator=">=", threshold=alert.threshold)],
                action_type=override.action_type,
                parameters=override.parameters,
                target_service=override.target_service if override.target_service != alert.service else None,
                source_incident=incident_id,
            )

        trace = ReflexionTrace(
            incident_id=incident_id,
            alert_summary=f"{alert.severity.value.upper()}: {alert.service} {alert.metric}={alert.value}",
            initial_hypothesis=hypothesis,
            proposed_action=f"{proposed.action_type.value} on {proposed.target_service}",
            outcome="rejected" if override is None else "overridden",
            human_override_reason=notes,
            applied_action=f"{override.action_type.value} on {override.target_service}" if override else None,
            extracted_invariant=invariant.rule,
            forbidden_actions=forbidden,
            learned_skill_name=skill.name if skill else None,
        )
        return trace, skill, invariant


def override_changes_type(proposed: RemediationAction, override: Optional[RemediationAction]) -> bool:
    """Forbid the proposed action only when the SRE rejected it or chose a different kind of action; merely
    adjusting parameters (e.g. fewer replicas) is not evidence the action type is wrong."""
    return override is None or override.action_type != proposed.action_type
