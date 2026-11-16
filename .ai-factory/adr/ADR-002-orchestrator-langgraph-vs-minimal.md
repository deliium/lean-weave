# ADR-002: Orchestrator — LangGraph vs Minimal State Machine

- Status: Accepted
- Date: 2026-11-08

## Context

Requirement: prefer LangGraph only if benefits justify complexity. Spikes validated LangGraph interrupt/resume/sqlite durability and a minimal SM with JSON persistence.

## Decision

**Adopt minimal deterministic state machine** for MVP 1 `WorkflowOrchestrator`. **Defer LangGraph** to a later milestone if branching/HITL middleware becomes necessary. Publish LangGraph transition design as deferred alternative (project-structure reference).

Go/no-go criteria used:

| Criterion | Result |
|-----------|--------|
| Durable pause/resume without LangGraph | Pass (JSON StateStore) |
| Linear plan→implement→verify→retry only | Yes |
| Dependency weight acceptable for MVP | LangGraph too heavy |
| Multi-agent/workers needed | No |

## Consequences

- Production code avoids langgraph runtime dependency (spikes may keep it in optional group).
- Revisit if product needs complex graphs; spike remains as evidence.
