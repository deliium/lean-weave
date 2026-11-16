# v1-01 State Schemas

Logged: `[Schemas.define]`. Version: **1.0.0**.  
Code: `src/lean_weave/schemas/models.py`.

## Entities

| Entity | Purpose |
|--------|---------|
| `FeatureState` | Top-level serializable workflow state (no chat history) |
| `FeaturePlan` | Goal, settings, reconciliation rows, tasks, commit checkpoints |
| `ImplementationTask` | Single task id/title/files/deps/status/outcome_digest |
| `VerificationResult` | pass/warn/fail aligned with `aif-gate-result` spirit |
| `TokenCounters` | Metrics hooks (ids/counters only) |

## Field semantics (highlights)

- `stage`: plan | implement | verify | retry | done | failed | interrupted
- `active_task_id`: sole implement focus for ContextProvider
- `interrupt`: opaque resume payload (reason, task id); cleared on resume
- `metrics`: cumulative token counters — never embeds prompts
- `outcome_digest`: short post-task summary for later stages (not full LLM output)

## Minimal SM transition table

| From | Event | To |
|------|-------|----|
| plan | planned | implement |
| implement | task_done | implement |
| implement | all_tasks_done | verify |
| implement | pause | interrupted |
| interrupted | resume | implement |
| verify | pass | done |
| verify | fail | retry |
| retry | requeue | implement |
| retry | exhausted | failed |

## LangGraph mapping (deferred alternative)

If LangGraph were adopted later, map:

- `FeatureState` fields → graph state TypedDict
- `tasks` / digests → channels with reducers (`operator.add` for event log only; overwrite for `stage`)
- `interrupt` → `langgraph.types.interrupt` + checkpointer `thread_id=feature_id`
- Prefer **not** storing full messages in graph state; keep tool-loop buffer ephemeral

MVP uses the explicit transition table above (ADR-002: defer LangGraph).
