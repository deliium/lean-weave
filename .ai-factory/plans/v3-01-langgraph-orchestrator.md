# Provisional Plan: LangGraph Feature Development Orchestrator

Plan ID: v3-01-langgraph-orchestrator  
Branch: none created (plan-only changes on `main`)  
Created: 2026-11-20  
Status: deferred — not implementation-ready

## Original Request

ADR-002 should remain authoritative for MVP 1.

Update v1-06-orchestrator to implement the lightweight orchestration approach defined in ADR-002 rather than introducing LangGraph prematurely.

Preserve the existing FeatureState, LLMProvider, ContextProvider, CodeExecutor, and VerificationRunner interfaces so that LangGraph can be introduced later without rewriting the core pipeline.

Move LangGraph-specific requirements, including StateGraph, interrupt handling, and LangGraph checkpointing, to a separate post-MVP plan provisionally named v3-01-langgraph-orchestrator.

Do not silently supersede ADR-002. If LangGraph is considered necessary for MVP 1, first propose an explicit ADR amendment explaining the technical justification, implementation cost, and alternatives.

Priority: Approved ADRs and RULES.md take precedence over previously generated implementation plans.

## Settings

- Testing: yes; offline by default.
- Logging: verbose; metadata only.
- Docs: yes; mandatory `/aif-docs` checkpoint if implementation proceeds.
- Dependencies: completed [v1-06-orchestrator](v1-06-orchestrator.md), validated MVP baseline, and approved ADR amendment or successor before runtime implementation.

## Roadmap Linkage

Milestone: "Optional LangGraph revisit"  
Rationale: Evaluate LangGraph only when measured branching or human-approval requirements justify replacing the minimal driver.

## Requirements Reconciliation

Authority: [ADR-002](../adr/ADR-002-orchestrator-langgraph-vs-minimal.md) and [RULES.md](../RULES.md) remain authoritative. [ADR-004](../adr/ADR-004-context-isolation-and-state.md) continues to govern persistence and stage isolation. The original `v1-06` request supplies deferred requirements, and [the LangGraph validation reference](../references/v1-01-langgraph-validation.md) is spike evidence, not approval to adopt. Keep LangGraph spike-only until the decision gate passes.

| Combination | Required behavior and persistence | Evidence |
|---|---|---|
| No approved amendment, or no demonstrated need | Keep MVP and dependencies unchanged; do not start the adapter. | ADR decision and existing dependency-policy tests. |
| Approved, opt-in graph driver; straight-through workflow | Explicit `StateGraph` nodes/conditional edges delegate to the same stage handlers; the same contracts, approvals, budget denials, deterministic retry and results apply. | Run the `v1-06` MockLLMProvider scenario matrix against both drivers. |
| Approved interrupt and resume | Interrupt only at a designated safe boundary. Validate decision, checkpoint identity, current approvals and budget before continuing; reject stale or cross-feature state. | In-memory interrupt tests and a fresh-process SQLite round trip. |
| Interrupted checkpoint or node restart | Reconcile cursor with authoritative `FeatureState`; never automatically repeat an unconfirmed edit or reset budget. | Crash injection at StateStore/checkpointer boundaries; duplicate/ambiguous effect tests in temporary workspaces. |

## Tasks

- [ ] Task 1: Prepare a go/no-go ADR proposal. Compare actual MVP branching and approval needs against the lightweight driver plus a native approval boundary, versus an opt-in LangGraph adapter. Include technical justification, spike limits, dependency/install cost, migration and replay risk, maintenance/test overhead, rollback and measurable adoption criteria. Use `.ai-factory/adr/ADR-002-orchestrator-langgraph-vs-minimal.md`, `spikes/langgraph_probe/probe.py`, and MVP behavior as evidence. Stop after the proposal until explicitly approved; do not edit the accepted ADR from this plan. Report decision evidence and risk in the proposal; no runtime logging changes.
- [ ] Task 2: Implement a thin opt-in `StateGraph` driver (depends on approved Task 1). Add `src/lean_weave/workflow/langgraph_adapter.py` and isolated optional dependencies in `pyproject.toml`/`uv.lock` only as approved. Define explicit prepare, plan, validate, implement, verify, diagnose/retry, and finalize nodes and deterministic edges; carry only feature identity, version/revision and bounded routing data, not messages or tool buffers. Reuse the existing `FeatureState`, all provider and tool interfaces, and MVP stage handlers. Keep the default install and driver free of graph imports; map graph outcomes to the same typed results. Log route labels and status at DEBUG/INFO.
- [ ] Task 3: Add LangGraph interrupts and durable graph checkpointing (depends on 2). Add `src/lean_weave/workflow/langgraph_checkpoint.py` and `tests/workflow/test_langgraph_checkpoint.py`. Wrap `interrupt()` and `Command(resume=...)` behind the existing resume interface; keep interrupt nodes free of side effects before the pause. Use an in-memory saver for tests and an approved per-workspace SQLite saver under the contained state directory for process restarts. Bind graph threads to workspace and feature identity; keep `StateStore` authoritative for business state and effect/budget records, so existing MVP checkpoints are not rewritten. Specify how graph cursor and product state revision reconcile after a crash; fail closed when replay safety is unknown. Log checkpoint IDs/revisions and sanitized refusal codes, not resume payloads.
- [ ] Task 4: Prove parity and document rollout (depends on 2–3). Extend `tests/workflow/` to exercise the same copied real-module fixture and MockLLMProvider scenarios with both drivers, plus interrupt/resume, checkpoint isolation, stale revision, duplicate-effect prevention, and rollback to the minimal driver from a safe boundary. Check that graph extras remain optional and no graph type leaks into shared contracts. At the docs checkpoint, update `docs/architecture.md` and `docs/getting-started.md` with opt-in, compatibility, rollback and checkpoint retention. Verify that logs contain only checkpoint/transition metadata and no prompts or secrets.
