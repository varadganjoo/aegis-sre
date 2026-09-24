"""SkillBank: procedural skills stored as validated data.

A skill pairs trigger conditions (metric, comparator, threshold, and which services) with one allowlisted action
and its parameters. Matching evaluates real thresholds and service scope, and learned invariants can veto a skill.
Nothing is compiled or executed: skills produce RemediationActions that go through the same validation and
blast-radius guard as any other proposal.

State is in-memory by default. Set AEGIS_STATE_DIR to persist skills as JSON between restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.actions import validate_action
from app.schemas import ActionType, Alert, ClusterTopology, Condition, SkillDefinition

logger = logging.getLogger("aegis_sre.skill_bank")

BUILTIN_SKILLS = [
    SkillDefinition(
        skill_id="skill-001",
        name="horizontal_pod_scaler",
        # CPU only: latency spikes are often caused by a dependency, so they go to deliberate diagnosis instead.
        description="Doubles replicas of a stateless service under CPU pressure.",
        stateless_only=True,
        conditions=[Condition(metric="cpu_utilization_pct", comparator=">", threshold=80.0)],
        action_type=ActionType.SCALE,
        parameters={"scale_factor": 2.0},
    ),
    SkillDefinition(
        skill_id="skill-002",
        name="circuit_breaker_tripper",
        description="Isolates a stateless service whose error rate would otherwise cascade to its callers.",
        stateless_only=True,
        conditions=[Condition(metric="error_rate_pct", comparator=">", threshold=10.0)],
        action_type=ActionType.CIRCUIT_BREAK,
        parameters={"duration_seconds": 300},
    ),
]


def _state_file(name: str) -> Optional[Path]:
    directory = os.getenv("AEGIS_STATE_DIR", "").strip()
    return Path(directory) / name if directory else None


class DynamicSkillBank:
    def __init__(self, topology: ClusterTopology, state_file: Optional[Path] = None):
        self.topology = topology
        self.state_file = state_file if state_file is not None else _state_file("skills.json")
        self._lock = threading.Lock()
        self._skills: Dict[str, SkillDefinition] = {s.name: s.model_copy(deep=True) for s in BUILTIN_SKILLS}
        self._load()

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                for item in json.loads(self.state_file.read_text(encoding="utf-8")):
                    skill = SkillDefinition(**item)
                    self._skills[skill.name] = skill
            except Exception as exc:
                logger.error("Ignoring unreadable skill state %s: %s", self.state_file, exc)

    def _persist(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps([s.model_dump(mode="json") for s in self._skills.values()], indent=2), encoding="utf-8")

    def list_skills(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def applies_to(self, skill: SkillDefinition, service: str) -> bool:
        node = self.topology.services.get(service)
        if node is None:
            return False
        if skill.services and service not in skill.services:
            return False
        return not (skill.stateless_only and node.is_stateful)

    def match_skill_for_alert(self, alert: Alert, forbidden: Iterable[ActionType] = ()) -> Optional[SkillDefinition]:
        """First skill whose service scope and a threshold condition match the alert, unless a learned
        invariant forbids its action on that service. Learned skills are checked before built-ins."""
        blocked = set(forbidden)
        ordered = sorted(self._skills.values(), key=lambda s: s.created_by != "sre-override")
        for skill in ordered:
            if skill.action_type in blocked or not self.applies_to(skill, alert.service):
                continue
            if any(c.matches(alert.metric, alert.value) for c in skill.conditions):
                return skill
        return None

    def register_skill(
        self,
        name: str,
        description: str,
        services: List[str],
        conditions: List[Condition],
        action_type: ActionType,
        parameters: Dict[str, Any],
        target_service: Optional[str] = None,
        created_by: str = "sre-override",
        source_incident: Optional[str] = None,
    ) -> SkillDefinition:
        """Validates the action against every service it can act on, then stores the skill as data."""
        for service in [target_service] if target_service else services:
            parameters = validate_action(action_type, service, parameters, self.topology)
        with self._lock:
            skill = SkillDefinition(
                skill_id=f"skill-{len(self._skills) + 1:03d}",
                name=name,
                description=description,
                services=services,
                conditions=conditions,
                action_type=action_type,
                parameters=parameters,
                target_service=target_service,
                created_by=created_by,
                source_incident=source_incident,
            )
            self._skills[name] = skill
            self._persist()
        return skill

    def record_success(self, name: str) -> None:
        with self._lock:
            if name in self._skills:
                self._skills[name].success_count += 1
                self._persist()
