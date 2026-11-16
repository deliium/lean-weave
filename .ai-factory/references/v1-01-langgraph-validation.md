# v1-01 LangGraph Checkpoint / Interrupt / Resume Validation

Logged: `[LangGraphProbe.step]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.  
Probe: `spikes/langgraph_probe/` · Tests: `tests/spikes/test_langgraph_probe.py` (**pass**).

## Scenarios validated

| Scenario | Checkpointer | Evidence |
|----------|--------------|----------|
| Pause + `Command(resume=...)` | `InMemorySaver` | `demo_interrupt_resume` — steps include prepare → gate:approve → work → done |
| Continue after error `invoke(None, config)` | `InMemorySaver` | `demo_continue_after_error` — work node called twice; graph completes |
| Durable resume across process boundary | `SqliteSaver` (`langgraph-checkpoint-sqlite`) | New saver instance + same sqlite file + `thread_id` resumes interrupt |

## Semantics observed

- **Interrupt:** `interrupt()` pauses the graph; state/checkpoint retained under `thread_id`.
- **Resume:** `Command(resume=payload)` injects value into the interrupted node and continues.
- **Node restart on error:** Failed node re-executes on `invoke(None, config)`; upstream completed work is not re-run as separate user-visible duplicates beyond reducer append semantics (`steps` uses `operator.add`).
- **Durable identity:** `(sqlite path, thread_id)` is sufficient to resume after process exit.

## Production checkpointer options (not added as deps)

| Option | Fit | Notes |
|--------|-----|-------|
| `InMemorySaver` | tests only | Lost on process exit |
| `SqliteSaver` | MVP local CLI | File under `.lean-weave/`; already spiked |
| Postgres checkpointer | later | Extra ops burden; defer |
| Redis checkpointer | later | Workers/multi-instance; out of MVP 1 |

## MVP implications

LangGraph satisfies Requirement 3 (checkpoint, interrupt, resume, durable sqlite). Adoption still depends on complexity tradeoff vs minimal SM (Task 7 / ADR-002).
