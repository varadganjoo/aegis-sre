# Aegis-SRE Design Notes

**Status:** Implemented (simulated cluster)
**Author:** Varad Ganjoo
**Updated:** September 2026

## 1. Problem

Automated remediation is useful for known failure modes and dangerous for new ones. A fixed runbook cannot handle an outage it has not seen, and an LLM with cluster access can take an action nobody would approve, such as restarting a primary database. Aegis explores a middle path: let a model propose, let deterministic code decide what is safe, and let a human make the call whenever either is unsure.

## 2. Goals and non-goals

Goals:
- Resolve known alert signatures without a model call.
- Get a dependency-aware diagnosis from an LLM for everything else, and never act on it without review.
- Make every action checkable before it runs: a closed set of actions with bounded parameters.
- Learn from SRE decisions so the same wrong proposal does not come back.

Non-goals:
- Real cluster access. Execution is simulated and returns a log line.
- Generating or running code. An earlier version synthesised Python remediation scripts and filtered them with an AST blocklist. Blocklists over a general-purpose language are hard to make complete, and operator notes could reach the generated source, so that design was removed.

## 3. Decisions

### 3.1 Skills are data
A skill is a record: conditions (`metric`, comparator, threshold), the services it covers, whether it is limited to stateless services, one action type and its parameters. Matching is a threshold comparison, never string matching or evaluation. Registering a skill runs the same validator as a model proposal, so a skill cannot hold an action the system would refuse to run.

### 3.2 One validator for every source of actions
Model proposals, SRE overrides, stored skills and MCP tool calls all pass through `app/actions.validate_action`: the action must be in the allowlist, the target must exist in the topology, stateful-only actions need a stateful target, failover needs a database, and parameters must match a strict pydantic model (`extra="forbid"`, numeric bounds). Anything else is rejected with a readable error.

### 3.3 The model proposes, code gates
The model returns a structured `DiagnosisProposal` (hypothesis, evidence, action, parameters, rollback, confidence). Only the parameters of the chosen action are kept. If the model is unreachable, over quota, or proposes something invalid, deterministic rules produce a conservative action instead (drain traffic for stateful services, scale out otherwise) and the reason is shown. Model output is never treated as trusted: every System 2 proposal goes to the SRE gate regardless of its blast radius.

Operator notes from earlier incidents are passed to the model JSON-quoted inside the history section, and the system instruction says notes are data. They are stored and displayed as text only.

### 3.4 Blast radius is deterministic
`app/blast_radius.py` walks the dependency graph (cycle-safe) to find every service that depends on the target, estimates traffic exposure, and applies fixed rules: learned invariants, stateful restarts and failovers, forced failover, `flush_all`, circuit breaking, loss of N+1 redundancy, traffic thresholds, and a minimum rollback plan. Scale-ups are exempt from the traffic rules because adding capacity interrupts nobody. Risk levels are compared by rank, not by name.

### 3.5 Human gate with LangGraph interrupts
The graph (`ingest -> system1 -> system2 -> guard -> sre_gate -> execute -> learn`) uses a typed state so each node merges a partial update. `sre_gate` calls `interrupt()` with the proposal and blast radius; the API resumes it with `Command(resume=...)`. Each diagnosis runs on its own thread id, so two runs of the same incident never share state.

### 3.6 Learning from decisions
- **Reject:** add an invariant forbidding the proposed action type on that service.
- **Override with a different action type:** same invariant, plus a learned skill (service scope, the alert's metric at its threshold, the SRE's action and target). Learned skills are matched before built-ins.
- **Override that only changes parameters:** learned skill and a rule note, but nothing is forbidden; the action type was right.

Invariants also veto matching skills, so a learned "never do X here" beats a built-in skill that would do X.

## 4. Threat model

| Risk | Mitigation |
| :--- | :--- |
| Model proposes a destructive or out-of-scope action | Allowlist, strict parameter bounds, blast-radius rules, mandatory review for System 2 |
| Prompt injection through alert text or operator notes | Notes are JSON-quoted data; model output is validated; nothing is executed from text |
| Operator override with bad input | Same validator as model proposals; the run stays paused on a 422 |
| Learned skill drifts beyond its incident | Skills are scoped to one service and one metric threshold; invariants can veto them |
| Model outage | Gemini fallback chain, then Groq, then deterministic rules |

## 5. Limits and next steps

- State is in process memory. A shared checkpointer (Postgres or Redis) and a skills table would be needed for more than one instance.
- Execution is simulated. A real executor would sit behind the same validator, run a dry run first, and record the rollback it would need.
- Learned skills do not generalise across services; clustering similar incidents is the obvious next step.
- There is no evaluation set of labelled incidents yet, so diagnosis quality is not measured.
