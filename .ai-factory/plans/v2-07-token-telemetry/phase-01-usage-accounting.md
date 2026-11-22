# Phase 1: Single-source usage accounting

Plan: [index.md](index.md)  
Tasks: 1–2  
Depends on: none

## Objective

Attribute actual gateway usage to workflow work without changing any budget decision.

## Current-Code Evidence

| Path | Symbols | Why it matters |
| --- | --- | --- |
| `src/lean_weave/metrics/models.py` | `normalize_usage`, `UsageCounters`, `JournalEvent`, `BudgetReservation`, `AttemptSummary` | DeepSeek reports nullable counters; cached and reasoning tokens are breakdowns. |
| `src/lean_weave/metrics/jsonl.py` | `reserve_attempt`, `finalize_attempt`, `_spent_from_events`, `_summary_from_events` | Fsynced reservations and paired outcomes are canonical, including errors and unknown outcomes. |
| `src/lean_weave/providers/deepseek.py` | `complete`, `_wire_request` | One reservation per transport attempt; response projection omits failed sends and vendor retries. |
| `src/lean_weave/workflow/tool_loop.py`, `src/lean_weave/workflow/budget.py` | `run_tool_loop`, `WorkflowBudget` | Caller knows task and strategy; scope spend is a separate admission view, not a second usage source. |

## Files to Change

| Path | Action | Required change |
| --- | --- | --- |
| `src/lean_weave/contracts/_types.py`, `src/lean_weave/providers/deepseek.py` | Modify | Carry validated local-only attribution to gateway reservations. |
| `src/lean_weave/workflow/tool_loop.py`, `src/lean_weave/workflow/orchestrator.py` | Modify | Tag actual dispatches after V2/fallback choice, from trusted workflow metadata. |
| `src/lean_weave/metrics/models.py`, `src/lean_weave/metrics/jsonl.py` | Modify | Extend existing records/replay; expose a consistent reporting snapshot without changing settlement. |
| `src/lean_weave/metrics/analytics.py` | Create | Pure aggregation over the collector's existing replayed attempts. |
| `tests/test_metrics_analytics.py` | Create | Ledger, gateway and reporting regression cases. |

## Task 1: Attribute journaled provider attempts

### Intent

Capture task and actual strategy even if the gateway receives no response or the process dies after reservation.

### Implementation Steps

1. Add `UsageAttribution` in `metrics/models.py` and a local `usage_attribution` key to `LLMRequest` (use a forward type reference if necessary to avoid the metrics/contracts import cycle). Attributes: null-or-task ID (existing plan ID contract, no free-form text), null-or-context strategy (`v1`, `v2`, `v1_fallback`), and null-or-positive stage-attempt ordinal (allocated in Task 3). Feature/stage/request/send identity and requested/returned model already exist; do not duplicate them. Benchmark case is attached by the runner to the resulting feature, not inferred from a feature ID.
2. In `run_tool_loop`, add trusted attribution after copying the guarded request and overriding its feature/stage and before calling `provider.complete`; add explicit strategy to all `MinimalWorkflowOrchestrator` plan/implement/retry call sites, including fallback. Use null task ID for plan; never recover IDs from messages. Non-workflow/probe callers remain untagged.
3. In `DeepSeekProvider.complete`, validate attribution before booking. Keep `AttemptJournal.reserve_attempt` unchanged and add a separate optional capability `reserve_attributed_attempt`, implemented by `JsonlMetricsCollector` as a delegate to the same locked booking helper. Call it only with valid attribution when the journal provides it; otherwise call the original method exactly once and report missing attribution. Do not catch `TypeError` and rebook: the first booking may already be durable. The wire projector must not see attribution. Put it on `BudgetReservation` and the reservation `JournalEvent`, not on every outcome; replay joins from the reservation, including unresolved ones.
4. Keep existing event identity and duplicate rules. Legacy rows read with null attribution; omit the new optional key on untagged serialization (preserving historical writer shape). New attributed rows need the upgraded reader; do not rewrite history or let an older binary resume a feature that wrote new rows.

### Required Interfaces and Contracts

- No changes to `charge_for_usage`, `spent`, workflow scope limits, packed guard/wire payload, retry policy, or transport timeout. Injected providers without the journal capability keep working; stats report unavailable rather than backfilling response tokens.
- Tagged sends have exactly one attribution across all transport retries for the same request. An unresolved reservation is an unknown send/outcome, not proof of a completed network request.

### Requirement Evidence

Original Request §§2–4 and acceptance 5–6. Tagged, untagged and interrupted sends are distinct cases in the reconciliation table.

### Error Handling and Logging

Reject invalid local attribution without transport; DEBUG stage/strategy/ordinal/presence only. Keep existing journal errors fail-closed. Never log prompts, raw usage objects, cache keys, source or credentials.

### Tests

In `tests/test_metrics_analytics.py` use temporary journals and `httpx2.MockTransport` (the provider's transport type) for failed response plus retry, terminal error carrying usage, legacy rows, exact duplicates, malformed/conflicting rows, unresolved reservation and sent JSON without attribution. Send a tagged request through an old-signature journal double and assert one booking; simulate an attributed append error and assert no fallback booking. Exercise orchestration attribution for plan, implement, retry and guarded fallback in Phase 2.

### Acceptance Criteria

The reservation has authoritative attribution before transport, including on interrupted runs, with no change in charged tokens or transmitted JSON.

### Verification

`uv run pytest tests/test_metrics_analytics.py -k 'attribution or wire or replay'` — all cases pass offline.

## Task 2: Aggregate attempts without double counting

### Intent

Publish one reusable projection for stats and benchmarks.

### Implementation Steps

1. In `JsonlMetricsCollector`, expose `reporting_snapshot(feature_id)` using the existing replay validator; include paired attempt summaries, preflight inputs, nullable counters, and budget charges. Do not create a second journal/aggregator for benchmarks. Preserve existing `summary()`/`totals()` output semantics. For the reporting read, only open existing files under the collector path policy; use a no-follow journal descriptor and a shared lock on the existing lock file, no directory/lock creation. Missing journal means unavailable; an existing journal without a usable lock is an error, not an unlocked read. Bound stats replay at 64 MiB and 100,000 events under lock; fail with a stable limit reason, never return a truncated total; do not apply limits to admission.
2. Implement `aggregate_usage(snapshot, group_by=...)` in `metrics/analytics.py`. Group by ordered combinations of feature, task, stage, `(requested_model, returned_model)`, actual strategy and external benchmark case; use explicit null/unknown buckets. Each eligible canonical row contributes once per chosen grouping. Keep legacy usage separately counted from transport attempts; exclude probe rows from workflow reporting.
3. Calculate per-row total from provider `total_tokens` when valid, otherwise from input + output when both present. Never add optional breakdowns to totals and never derive a total by combining partial counters from different attempts. Keep per-counter coverage and distinguish derived versus reported totals; flag contradictory reported totals versus input + output without repairing them. Null means unavailable; actual zero remains zero. Display known sums with coverage, not as full totals when relevant attempts are unknown.
4. Report distinct logical gateway calls (unique `(feature_id, request_id)` pairs counted independently in each group, not summed from child groups), finalized local attempts, provider retry attempts (positive ordinals, separately unresolved), failed terminal attempts and their known usage, unresolved reservations, summed provider-attempt elapsed time, and disjoint measured-settled, estimated-settled, and unresolved budget charges that reconcile to `spent()`. Derive the buckets from rows, not mixed legacy feature/stage subtotals; account for historical `legacy_usage` charges under an explicit legacy settled subtotal included in reconciliation, not as gateway attempts. Task/stage scoped budget counters are read from the checkpoint in Phase 2, not synthesized by summing provider charges.

### Required Interfaces and Contracts

- Deterministic typed, JSON-ready rows; nullable counters, field coverage and budget units explicitly named. Keep compatibility `TokenUsage` zero-filling out of the new reporting path.
- A failed workflow attempt can contain successful provider sends: Phase 2/4 join journal rows by stage attempt and project failed workflow work separately from failed provider sends.

### Requirement Evidence

Original Request §§2–4 and token-efficiency bullets. Verify partial, full, mixed outcome and interrupted groups without fan-out or imputation.

### Error Handling and Logging

Propagate journal integrity failures; don't silently return zero. DEBUG report dimension/counts only, WARN on incomplete coverage with stable reason code, never emit raw journal rows.

### Tests

In `tests/test_metrics_analytics.py` assert `input=10, output=6, total=16, cache=4, reasoning=2` gives 16; mix null/zero/partial/reported-only/contradictory usage and verify coverage, legacy handling, grouped conservation of additive counters, failed and unresolved counts, separate retry counters and no writes on stats reads. For a failed attempt with no returned model followed by a retry that returns a model, assert one logical call overall and one within each relevant model group.

### Acceptance Criteria

Feature and grouped processed totals agree for the same known attempts, and budget behavior is byte-for-byte unchanged for equivalent reservations.

### Verification

`uv run pytest tests/test_metrics_analytics.py` — aggregation and transport tests pass.

## Phase Risks and Mitigations

- New fields in a strict journal schema can block downgrade; document forward-reader compatibility and preserve old-format rows without rewriting.
- Confusing gateway and workflow accounting inflates totals; report each view from its existing source and label them.

## Phase Completion Checklist

- Both tasks meet acceptance; offline verification passes.
- Mark Tasks 1–2 in `index.md` after verification.
