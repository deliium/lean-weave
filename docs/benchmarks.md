[← Worktree workflow](worktree-workflow.md) · [Back to README](../README.md)

# Benchmarks and Token Baseline

Developer tooling under `benchmarks/` validates the MVP feature loop and
records a reproducible token baseline. It is **not** a `wl` product command.

## Fixture corpus

Seven pinned cases live in `benchmarks/fixtures/`:

| ID | Intent |
|----|--------|
| `bug_fix` | Simple one-file repair |
| `single_file_feature` | Single-file feature edit |
| `multi_file_feature` | Ordered dependent tasks |
| `test_repair` | Repair a weakened test without deleting it |
| `verify_then_repair` | Fail verification, then repair |
| `insufficient_context` | Out-of-policy plan without widening access |
| `budget_exhaustion` | Stop on tiny token budgets |

Each manifest pins `expected_head`, request text, write paths, check IDs, and
pass criteria. Oracles and intended edits stay **outside** the materialized
Git tree so they never enter model context. Materialization uses fixed Git
identity/timestamps and never initializes the developer checkout.

```bash
uv run --locked pytest tests/benchmarks/test_fixtures.py
```

## Offline evidence (default)

In-process CLI evidence injects `httpx2.MockTransport` at the DeepSeek HTTP
seam so reservations settle into the real per-worktree journal:

```bash
uv run --locked pytest tests/cli/test_feature_offline_e2e.py \
  tests/cli/test_feature_recovery_e2e.py \
  tests/cli/test_feature_isolation_e2e.py \
  tests/benchmarks/
```

`benchmarks/offline.py` builds operator configs and scripted completions.
`benchmarks/results.py` projects journal + checkpoint metrics without inventing
zeros for missing fields and without double-counting cache/reasoning subsets.

### Metric units

| Field | Meaning |
|-------|---------|
| `prompt_tokens` / `completion_tokens` / `total_processed_tokens` | Provider-reported counters when present |
| `cache_hit_tokens` / `reasoning_tokens` | Optional subsets; not added back into totals |
| `estimated_input_tokens` | Reservation preflight estimate (not a send) |
| `budget_charge_tokens` | Conservative charge used for admission |
| `logical_workflow_calls` | Confirmed outcomes only (not unresolved reservations) |
| `gateway_retry_attempts` | Transport attempt ordinals `> 0` |
| `workflow_repair_count` | From `FeatureState.retry_count` (stage `retry` is repair, not transport retry) |
| `context_size` | Initial `ContextPack.estimated_tokens` when observed; otherwise a labeled gap |
| `checks` | Observed check execution history when available; final `VerificationResult` alone is one batch |

## Live DeepSeek (explicit only)

Live runs require **all** of:

1. `--live` on `python -m benchmarks.run`
2. `DEEPSEEK_API_KEY` in the environment (never in the config file)
3. `--live-config` pointing at a trusted external operator config
4. Canonical endpoint `https://api.deepseek.com`
5. Exact fixture write paths, check IDs, argv, and data-only classification
6. Bounded case selection under `--aggregate-ceiling`

Key presence alone never enables live mode. Custom endpoints, wider write
allows, or workspace-code checks are refused before any child `wl` launch.

```bash
# Offline note only (in-process evidence stays in pytest):
uv run python -m benchmarks.run --cases bug_fix --output benchmarks/baselines

# Explicit live (operator-controlled):
uv run python -m benchmarks.run --live \
  --live-config /secure/operator.yaml \
  --cases bug_fix \
  --output benchmarks/baselines \
  --aggregate-ceiling 50000 \
  --deadline-s 120
```

## V2 comparison protocol

Paired V2 runs must keep:

- Identical fixture revisions and user requests
- Equivalent provider model/settings (thinking, caps)
- Matched check commands/oracles and execution limits
- Comparable run counts and environment
- All failed and uncertain attempts retained in the journal

Report mock-functional, local-estimate, and live DeepSeek results separately.
Disclose missing coverage and cache/time conditions. Claim savings only against
measured comparable results that also report correctness denominators.

## Redaction

Persisted reports store bounded counts and allowlisted metadata only. They never
store raw prompts, responses, patches, full config bodies, or environment values
that may contain secrets.
