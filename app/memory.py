"""Experiential memory: incident episodes and institutional invariants.

Invariants are structured: `forbidden_actions` are enforced by the blast-radius guard and veto System 1 skills,
and `rule` text is given to the diagnosis model as context. State is in-memory by default; set AEGIS_STATE_DIR
to persist it as JSON between restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.schemas import ActionType, Alert, Invariant

logger = logging.getLogger("aegis_sre.memory")

SEED_INVARIANTS = [
    Invariant(
        service="postgres-primary",
        rule="Never restart postgres-primary directly; use a planned switchover to the replica.",
        forbidden_actions=[ActionType.RESTART],
    ),
    Invariant(
        service="redis-cluster",
        rule="Never flush all Redis keys during peak traffic; use scan-based deletion.",
    ),
    Invariant(
        service="api-gateway",
        rule="Rate-limit surges should be paired with longer edge-cache TTLs to shield upstream services.",
    ),
]

MAX_EPISODES = 200


class ExperientialMemoryKernel:
    def __init__(self, state_file: Optional[Path] = None):
        directory = os.getenv("AEGIS_STATE_DIR", "").strip()
        self.state_file = state_file if state_file is not None else (Path(directory) / "memory.json" if directory else None)
        self._lock = threading.Lock()
        self.episodes: List[Dict[str, Any]] = []
        self.invariants: List[Invariant] = [inv.model_copy(deep=True) for inv in SEED_INVARIANTS]
        self._load()

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.episodes = data.get("episodes", [])
                self.invariants = [Invariant(**i) for i in data.get("invariants", [])] or self.invariants
            except Exception as exc:
                logger.error("Ignoring unreadable memory state %s: %s", self.state_file, exc)

    def _persist(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"episodes": self.episodes, "invariants": [i.model_dump(mode="json") for i in self.invariants]}
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record_episode(self, episode: Dict[str, Any]) -> None:
        with self._lock:
            self.episodes = (self.episodes + [episode])[-MAX_EPISODES:]
            self._persist()

    def add_invariant(self, invariant: Invariant) -> Invariant:
        with self._lock:
            for existing in self.invariants:
                if existing.service == invariant.service and existing.rule.strip().lower() == invariant.rule.strip().lower():
                    existing.forbidden_actions = sorted(
                        set(existing.forbidden_actions) | set(invariant.forbidden_actions), key=lambda a: a.value
                    )
                    self._persist()
                    return existing
            self.invariants.append(invariant)
            self._persist()
            return invariant

    def invariants_for(self, service: str) -> List[Invariant]:
        return [inv for inv in self.invariants if inv.service in (service, "*")]

    def forbidden_actions_for(self, service: str) -> set[ActionType]:
        return {action for inv in self.invariants_for(service) for action in inv.forbidden_actions}

    def similar_episodes(self, alert: Alert, limit: int = 5) -> List[Dict[str, Any]]:
        matches = [
            ep for ep in self.episodes
            if ep.get("alert", {}).get("service") == alert.service or ep.get("alert", {}).get("metric") == alert.metric
        ]
        return matches[-limit:]
