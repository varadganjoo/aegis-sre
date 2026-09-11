"""ExpeL (Experiential Learning) Memory Kernel.
Maintains episodic incident trajectories, institutional invariants, and counterfactual reflection records.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.schemas import Alert, IncidentState, ReflexionTrace

logger = logging.getLogger("aegis_sre.memory")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MEMORY_FILE = DATA_DIR / "memory_store.json"


class ExperientialMemoryKernel:
    """Stores episodic incident traces and extracts semantic invariants over time."""

    def __init__(self, data_file: Optional[Path] = None):
        self.data_file = data_file or MEMORY_FILE
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.episodes: List[Dict[str, Any]] = []
        self.invariants: List[Dict[str, str]] = []
        self._load()

    def _load(self):
        """Loads memory store from disk."""
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.episodes = data.get("episodes", [])
                    self.invariants = data.get("invariants", [])
            except Exception as e:
                logger.error("Failed to load experiential memory: %s", e)

        if not self.invariants:
            self._seed_initial_invariants()

    def _seed_initial_invariants(self):
        """Seeds foundational distributed system invariants."""
        self.invariants = [
            {
                "service": "postgres-primary",
                "invariant": "Never restart postgres-primary directly; must execute zero-downtime switchover via repmgr.",
                "source": "initial_sre_policy",
            },
            {
                "service": "api-gateway",
                "invariant": "Rate-limit 429 surges must be accompanied by edge-cache TTL extensions to shield upstream services.",
                "source": "initial_sre_policy",
            },
            {
                "service": "redis-cluster",
                "invariant": "Never flush all Redis keys during peak traffic; execute scan-based asynchronous deletion.",
                "source": "initial_sre_policy",
            },
        ]
        self._save()

    def _save(self):
        """Persists memory store to disk."""
        try:
            data = {"episodes": self.episodes, "invariants": self.invariants}
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to persist experiential memory: %s", e)

    def record_episode(self, state: IncidentState):
        """Records a completed or overridden incident episode."""
        episode = {
            "incident_id": state.incident_id,
            "title": state.title,
            "alert": state.alert.model_dump(),
            "cognition_mode": state.cognition_mode,
            "root_cause": state.root_cause_hypothesis,
            "proposed_action": state.proposed_action.model_dump() if state.proposed_action else None,
            "sre_approved": state.sre_approved,
            "sre_override_notes": state.sre_override_notes,
            "execution_result": state.execution_result,
            "reflexion": state.reflexion.model_dump() if state.reflexion else None,
            "created_at": state.created_at,
        }
        self.episodes.append(episode)

        # If a reflexion trace exists with an extracted invariant, store it
        if state.reflexion and state.reflexion.extracted_invariant:
            self.add_invariant(
                service=state.alert.service,
                invariant=state.reflexion.extracted_invariant,
                source=f"reflexion_{state.incident_id}",
            )
        self._save()

    def add_invariant(self, service: str, invariant: str, source: str):
        """Adds a learned invariant to the semantic knowledge base."""
        # Avoid duplicate invariants
        for inv in self.invariants:
            if inv["service"] == service and inv["invariant"].strip().lower() == invariant.strip().lower():
                return

        self.invariants.append({"service": service, "invariant": invariant, "source": source})
        self._save()

    def query_invariants_for_service(self, service: str) -> List[str]:
        """Retrieves learned invariants for a target service."""
        return [inv["invariant"] for inv in self.invariants if inv["service"] == service or inv["service"] == "*"]

    def query_similar_episodes(self, alert: Alert) -> List[Dict[str, Any]]:
        """Finds past episodes matching service or metric patterns."""
        matches = []
        for ep in self.episodes:
            ep_alert = ep.get("alert", {})
            if ep_alert.get("service") == alert.service or ep_alert.get("metric") == alert.metric:
                matches.append(ep)
        return matches[-5:]  # Return most recent 5 matches
