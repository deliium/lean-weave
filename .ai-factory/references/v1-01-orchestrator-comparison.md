# v1-01 Orchestrator Comparison (ADR-002 evidence)

Logged: `[OrchestratorCompare.evaluate]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.  
Inputs: Task 3 methodology map · Task 6 LangGraph spike · `spikes/minimal_sm_probe/`.  
Tests: `tests/spikes/test_minimal_sm_probe.py` + LangGraph spike tests (**pass**).

## Shared workflow under test

`plan → implement → verify → retry` with pause/resume and persisted FeatureState (JSON).

## Comparison table

| Dimension | LangGraph StateGraph | Minimal Python SM | Winner |
|-----------|----------------------|-------------------|--------|
| Dependency weight | `langgraph` + checkpoint + transitive `langchain-core` (~many pkgs) | stdlib + pydantic later | **Minimal** |
| Interrupt/resume ergonomics | First-class `interrupt` / `Command(resume)` + checkpointers | Explicit `interrupt` field + reload JSON | LangGraph |
| Durable resume evidence | SqliteSaver process restart validated (Task 6) | JSON file reload validated | Tie (both proven) |
| Observability | Graph state snapshots / next nodes | Structured logs + stage enum | Tie (MVP needs logs either way) |
| Testability | Needs checkpointer fixtures | Pure functions + JSON fixtures | **Minimal** |
| Token-irrelevant complexity | Graph reducers, node restart semantics, versioned checkpoints | Explicit transition table | **Minimal** |
| Fit MVP 1 (no workers/multi-agent) | Capable but oversized for linear pipeline | Matches linear methodology map | **Minimal** |
| Approx spike LOC | ~200 | ~180 | Tie |

## Recommendation

**Defer LangGraph for MVP 1 production orchestrator; adopt a minimal deterministic state machine** owning `FeatureState` transitions.

Rationale: LangGraph’s interrupt/checkpoint benefits are real (validated) but do **not** justify the dependency and cognitive overhead for a single linear workflow with no fan-out agents or workers. Durable resume is achievable with StateStore JSON under `.lean-weave/`. Revisit LangGraph if a later milestone needs branching graphs, human-in-the-loop middleware ecosystems, or multi-thread tool fan-out.

**Hybrid note:** keep `spikes/langgraph_probe/` as a living reference; Task 11 still publishes a LangGraph state/transition design as the deferred alternative so plan deliverables remain satisfied.

## Scores (subjective 1–5, higher = better for MVP)

| Option | Deps | Resume | Simple | MVP fit | Total |
|--------|------|--------|--------|---------|-------|
| LangGraph | 2 | 5 | 2 | 3 | 12 |
| Minimal SM | 5 | 4 | 5 | 5 | **19** |
| Hybrid (SM now, LG later) | 4 | 4 | 4 | 5 | 17 |

Recommended option for ADR-002: **minimal SM (defer LangGraph)**; hybrid acceptable as packaging of the same decision.
