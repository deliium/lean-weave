[← Worktree workflow](worktree-workflow.md) · [Back to README](../README.md)

# Benchmarks and Token Baseline

The canonical benchmark core now lives in the installed
`lean_weave.benchmark` package. `benchmarks/` remains a compatibility wrapper for
developer imports and historical baselines. The same packaged fixtures and
deterministic checks power `wl benchmark`; the workflow feature path is
unchanged and V1 context remains the default.

## Installed commands

```bash
# Deterministic offline workflow suite (default: workflow)
wl benchmark run --suite workflow --output /secure/results/run.json

# Retrieval relevance and packed-source evaluation
wl benchmark run --suite retrieval --output /secure/results/retrieval.json

# Human-readable report (Markdown to stdout by default)
wl benchmark report /secure/results/run.json

# Paired comparison within one named axis
wl benchmark compare baseline.json candidate.json --axis strategy --json
```

`run` refuses an existing output path and publishes through an exclusively
created sibling plus a same-directory hard link; it never replaces an existing
file. `compare` and `report` never contact the provider. Filters may be repeated
with `--case` and `--variant`; `--suite all` rejects those filters. Repeated
runs are bounded to 1–10.

### Comparison eligibility

Only one declared condition may differ per axis:

| Axis | Allowed difference |
| --- | --- |
| `strategy` | V1 default versus V2 default |
| `map` | graph/map switch |
| `budget` | named pack budgets (for example `v2-tight`) |
| `cache` | labeled local cache state |
| `revision` | tool implementation revision |

The full schedules are paired by `(suite, fixture, repetition)` first; a
missing arm is incompatible, never silently dropped. Provenance, fixture SHA or
tree fingerprint, origin HEAD, verifier/limits, model settings and outcome
contract must match. Incomplete or failed arms stay in the correctness
denominator. Token deltas require both arms to be correct and fully covered for
that field. No percentage delta is computed from zero.

### Regression thresholds

```json
{
  "version": 1,
  "gates": [
    {
      "metric": "token_delta_median",
      "provenance": "offline_scripted",
      "scope": "suite",
      "max_regression": 0
    }
  ]
}
```

Pass `--thresholds FILE` to `wl benchmark compare`. Metric names cover file and
symbol P@K/R@K, selected source tokens, missing dependency count, covered
processed tokens per verified task, correctness/success rate, retry frequency
and duration. Missing coverage is **unevaluable** and fails the requested gate.
Exit codes: `0` valid non-regressing evidence, `2` invalid/incompatible
options, `1` incorrect/incomplete run or failed/unevaluable gate. Expected
negative fixtures that meet their exact contract are not failures. No policy
means deltas are displayed without invented criteria.

### Sources of measurement uncertainty

- Provider totals include cache/reasoning subsets; those are never added again.
- Absent provider counters stay `unavailable`, not zero.
- `estimated_input_tokens` and budget charge are conservative local estimates,
  not measured API spend.
- Resolved local cache hits are not automatic API savings; provider cache-hit
  counters are reported separately only when the provider reports them.
- An unresolved reservation is an unknown send/outcome, not proof of a network
  request.
- Workflow/arm elapsed time and summed gateway-attempt latency are independent.
- Holdout relevance sets are small; scores are descriptive and disclose
  denominators.
- The historical `verify_then_repair` baseline had no retry and is not new
  evidence of repair correctness. No live baseline is asserted.
- Live model output, remote cache and latency are uncontrolled. Live token
  counts are provider-reported evidence, not an invoiced-cost guarantee.

## Telemetry and stats

`wl stats` and `wl stats feature <id>` are read-only projections over the
existing attempt journal and checkpoint. They add no second token store and
never start a workflow.

```bash
wl stats --json
wl stats feature <feature-id> --json
```

They expose feature/stage/task/model/strategy/case breakdowns, verification
outcome, both retry counters, bounded context observations (selected files and
symbols, pack estimates, expansion, cache hits/misses, retrieval time), budget
charge and two clocks. In-flight or legacy records may be marked partial. A
missing journal is unavailable evidence, not a zero total; corrupt evidence
fails with a stable error instead of a truncated total.

New attributed journal rows add an optional `attribution` object to the
reservation/outcome event. The reader accepts legacy rows with no attribution and
keeps their writer shape unchanged. Older binaries that do not understand the
new key must not resume a feature that wrote attributed rows; forward-reader
compatibility is one-way. Checkpoints may drop oldest telemetry detail on size
pressure before ever failing a previously valid state, so very old history can
legitimately report as truncated.

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

## Retrieval quality comparison

Labeled V1 vs V2 ranking cases live under
`benchmarks/fixtures/retrieval/` and are scored by
`python -m benchmarks.retrieval` (not a `wl` command). See
[context-retrieval.md](context-retrieval.md) for metrics, limitations, and
commands:

```bash
uv run --locked python -m benchmarks.retrieval --json
uv run --locked pytest tests/benchmarks/test_retrieval.py
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

Live work is opt-in and requires **all** of:

1. `--live` on `wl benchmark run`
2. `DEEPSEEK_API_KEY` in the environment (never in the config file)
3. `--live-config-dir` with a case-ID-named external operator config
4. Canonical endpoint `https://api.deepseek.com`
5. Exact fixture write paths, check IDs, argv, and data-only classification
6. Explicit `--max-tokens`, `--max-cost-usd`, `--pricing`, and `--deadline-s`
7. Selected positive cases only (`bug_fix`, `multi_file_feature` initially)

Key presence alone never enables live mode. Custom endpoints, wider write
allows, workspace-code checks, unsupported cases/models, proxy/override
environment variables, malformed prices, or missing limits are refused before
any billable child launch. Repeats are capped at 10. Pricing is strict JSON:

```json
{
  "version": 1,
  "currency": "USD",
  "as_of": "YYYY-MM-DD",
  "source": "documented provider price list",
  "models": {
    "deepseek-flash": {
      "uncached_input_per_million": "0.00",
      "output_per_million": "0.00"
    }
  }
}
```

Admission reserves the entire batch allowance before launch: the existing
per-arm feature-token cap times the highest supplied USD/token rate for every
repetition, using fixed decimal arithmetic and upward rounding. These are
conservative **admission** controls, not exact remote billing guarantees.
Timeout billing, provider count differences and cache discounts are not assumed.
Live estimates remaining in a result stay labeled as local estimates.

```bash
# Legacy developer runner remains available for historical workflows:
uv run python -m benchmarks.run --cases bug_fix --output benchmarks/baselines

# Installed live preflight (requires external configs and limits):
wl benchmark run --live --suite workflow \
  --case bug_fix --live-config-dir /secure/live-configs \
  --max-tokens 50000 --max-cost-usd 1.00 \
  --pricing /secure/pricing.json --deadline-s 120 \
  --output /secure/results/live.json
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

The workflow-context projection labels each report with `context_mode` and
adds `estimated_input_tokens` per stage. `compare_workflow_context` requires
matched successful runs, retains per-stage deltas, and reports missing estimates
as `None` rather than zero. The checked-in paired fixture is:

```bash
uv run --locked pytest \
  tests/cli/test_feature_context_e2e.py \
  tests/benchmarks/test_workflow_context.py
```

Both arms use a fresh materialized Git repo, external trusted operator config,
the same pinned manifest/checks/request/model/limits, and the real offline
gateway journal. The fixture assertion is a strict reduction in the journal's
`estimated_input_tokens` on `multi_file_feature`; this is a local preflight
estimate, not API-reported input tokens. Scripted provider usage remains
`mock_functional`, packing counters remain separate local observations, and no
live-API claim is implied.

## Context-cache workload

`uv run python -m benchmarks.context_cache --json` reports disabled,
empty-enabled, and warm modes separately with and without a prebuilt index. It
reports local wall/CPU time, call counters, per-kind hits, and DB bytes, with
`llm_calls=0` and `api_tokens_measured=false`. See
[context-cache.md](context-cache.md).

## Redaction

Persisted reports store bounded counts and allowlisted metadata only. They never
store raw prompts, responses, patches, full config bodies, or environment values
that may contain secrets.
