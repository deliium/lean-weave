# Phase 1: Comparable evidence

Plan: [index.md](index.md)  
Tasks: 1–3  
Depends on: implemented v2-01 through v2-07

## Objective

Make offline evidence reproducible and difficult to misinterpret before changing any selection policy.

## Current-Code Evidence

| Path | Symbols / evidence | Why it matters |
|---|---|---|
| `benchmarks/baselines/v1-offline-baseline.{json,summary.txt}` | Revision `a7dca8722b3e4b0f2d79ca8324f972bc5d0bbfb5`; seven cases, 7/7 old correctness, zero repair retries, live not run | Historical only; recreate V1 from the current implementation and pinned fixtures. |
| `src/lean_weave/benchmark/runner.py` | `run_offline_arm`, `_negative_matches`, `run_matrix` | Auto sessions use real mock gateway/journal; current negatives accept overly broad outcomes; conditions omit effective verifier/settings and elapsed time stops before oracle. |
| `src/lean_weave/workflow/stats.py` | `project_feature_stats`, `_context_summary`, `_attempt_payload` | Checkpoint already has stage verification history, context observations, repeats and expansions. |
| `src/lean_weave/benchmark/compare.py` | `compare_runs`, `_axis_pair`, `_metric_values`, `_evaluate_gate` | Workflow-only row filter; `all(coverage.values())` excludes valid totals when optional subsets are absent; threshold provenance/scope not used in evaluation. |
| `src/lean_weave/cli/commands/benchmark.py` | `benchmark_run`, `benchmark_compare`, `benchmark_report`, `_publish` | Existing CLI and exclusive result publishing must remain usable. |

## Files to Change

| Path | Action | Responsibility |
|---|---|---|
| `src/lean_weave/benchmark/{runner,models,results,compare,report}.py` | Modify | Stable condition capture, bounded evidence projection, comparison and rendering. |
| `src/lean_weave/cli/commands/benchmark.py` | Modify | Preserve old arguments and exit meanings; show gaps and relevant arm outcomes. |
| `tests/benchmarks/{test_workflow_matrix,test_results,test_regressions,test_benchmark_cli}.py` | Modify | Regression coverage at runner, comparator and CLI boundaries. |

## Task 1: Pin benchmark conditions

### Intent
Use the same source and execution contract for each pair and catch incompatible evidence before interpreting deltas.

### Implementation Steps
1. In `benchmark/runner.py::run_offline_arm`, compute a fixture content fingerprint from its pinned HEAD plus canonical manifest, check/oracle/response-script identity, and ordered scenario definition. Record `origin_head` from the materialized checkout and reject mismatch with the pin. Reuse `benchmark/fixtures.py::materialize` and the existing external operator config flow; do not edit the historical corpus.
2. Record effective conditions before dispatch: task request digest, expected contract, operating mode, normalized verifier IDs/argv/content hashes/classification/timeouts/output caps, approval path set, model/thinking/output limits/retry settings, workflow/tool budgets, Python/Git versions, dependency-lock digest, and effective context knobs; otherwise record an explicit evidence gap. Replace sandbox-specific path prefixes with stable fixture/oracle IDs, not basenames alone. Use canonical JSON with sorted keys and SHA-256; no raw config.
3. Keep `ArmRow`/`BenchmarkRun` schema v1 readable by using existing dictionaries for additive evidence. Populate `spec.schedule` before execution with ordered `(suite, case_id, variant, repetition)` entries; reject duplicate case or variant selectors, derive order independently of persisted row order, and retain one row per entry. Alternate order by repetition (V1 first on odd, V2 first on even) for both offline and paired live. Give each arm fresh private scratch. For `--suite all`, combine the two declared schedules but permit only single-suite gate evaluation.
4. Distinguish fixture HEAD from package source revision and lock digest. A source run must record the verified source tree identity, not an unrelated current working directory's HEAD; an installed-wheel run outside Git must record the installed package/build identity instead. For release evidence, Task 9 binds that wheel's SHA-256 to its source revision and lock digest at build time; absent a binding, mark release eligibility unavailable instead of treating `unknown` as a matching revision.

### Required Interfaces and Contracts
An arm is keyed by suite/case/variant/repetition; a pair by suite/case/repetition. Context differences must be the only varying functional condition on strategy axis. Host identity details stay count/version-only; no absolute user paths or secrets. Unknown or old evidence remains readable, but cannot qualify as release evidence.

### Requirement Evidence
Original Request §§3–4 and Benchmark requirements; `docs/benchmarks.md` → Comparison eligibility; packaged `multi_file_feature` and `verify_then_repair` are required real artifacts.

### Error Handling and Logging
Retain one error or not-started row per scheduled arm. DEBUG stable condition digest and mode, WARN condition capture failure by safe code, INFO schedule counts; no prompts or config dumps.

### Tests
In `test_workflow_matrix.py`, run both modes over one real manifest twice and compare pins/conditions, normalize independent temporary roots, and reject altered check/limit, model, and source revision; simulate a fixture setup failure and assert the row survives.

### Acceptance Criteria
A reviewer can tell whether two arms used the same fixture, effective execution settings and verification contract without accessing operator config or local paths.

### Verification
`uv run --locked pytest tests/benchmarks/test_fixtures.py tests/benchmarks/test_workflow_matrix.py` passes.

## Task 2: Preserve bounded attempt evidence

### Intent
Capture enough read-only evidence to explain spend, retries, expansion, failures, and final success without a second product ledger.

### Implementation Steps
1. Add a benchmark-only projector in `benchmark/results.py` over the existing `ReportingSnapshot` and `ControlRecord`; invoke it before cleanup in `run_offline_arm` and reuse it for live in Task 7. Persist allowlisted attempt metrics (stage/task/call ordinal/transport retry, outcome class, input/output/total counters with coverage, estimate, charge and latency) and stage verification/context observations from `closed_attempts`. Sanitize to known enums and numeric fields; mark truncated observations. Never copy source, messages, raw diagnostics, tool payloads, config, or raw journal bytes.
2. Preserve terminal checkpoint state, oracle boolean, blocking check result and observed failure/repair sequence separately. Record session wall, workflow active and gateway-attempt time separately; record end-to-end wall including verification oracle/collection explicitly, not as the old session field. An unresolved reservation is not a confirmed send or zero. Add a benchmark dispatch-evidence value of `no_send`, `observed`, or `unknown`; accept zero incurred spend for `no_send` only with independent transport/ledger evidence, not an absent journal. Keep raw absent fields null. Use `metrics/analytics.py` counters to distinguish reported, derived and contradictory totals; never label a derived total provider-reported, and contradictions cannot qualify as covered measured totals.
3. Tighten `_negative_matches`: `budget_exhaustion` requires `budget_exhausted`, `feature_token_limit`, and no dispatch; `insufficient_context` requires `failed`, `invalid_plan`, and no approved writes or checks. The repair fixture requires blocking failure before repair and pass after it; full failure evidence remains in comparison denominators even if metric collection fails late.
4. In `benchmark/report.py` and CLI reporting, preserve schedule/coverage metadata and show workflow successes distinct from expected-negative correctness, logical calls distinct from transport attempts and workflow repairs, expansion attempts/deliveries, final checks, failed attempts and unresolved reservations. Keep historical JSON readable.

### Required Interfaces and Contracts
No new checkpoint fields, ledger, persistence authority, or raw payload retention. Provider cache/reasoning counters are subsets, not additive. Label scripted usage as scripted, local estimates as estimates, and live provider counters as provider reported. Limit raw numeric evidence to existing workflow/run caps and CLI size limits; overflow is incomplete, never a silent successful truncation.

### Requirement Evidence
Original Request §§2–4, 7, 9; `workflow/stats.py` and `docs/benchmarks.md` → Telemetry and stats, Metric units. Evidence includes the existing `verify_then_repair` and denied-access negative fixture.

### Error Handling and Logging
DEBUG counts/coverage and normalized phase/outcome enums only; WARN corrupt/missing/truncated structured evidence and keep the arm. Do not write debug source text.

### Tests
In `test_results.py`, cover mixed complete/partial attempts, unresolved reservations, token subsets, retries, and secret-free output. In `test_workflow_matrix.py`, assert exact negative statuses/reasons, real failed-then-passed checks, and V2 context observations rather than inferring them from config. Test report loading existing version-1 rows.

### Acceptance Criteria
Every present token count is labeled and traceable to its attempt and coverage; failures and closed-attempt/expansion gaps are visible; production StateStore is unchanged.

### Verification
`uv run --locked pytest tests/benchmarks/test_results.py tests/benchmarks/test_workflow_matrix.py tests/benchmarks/test_benchmark_cli.py` passes.

## Task 3: Enforce paired comparison eligibility

### Intent
Fix known evaluation gaps so a release gate cannot pass by dropping failures or unsupported data.

### Implementation Steps
1. In `compare.py::compare_runs`, independently validate each selected arm schedule from `spec.schedule`; build denominators from those declarations, not from present rows. A missing row yields an incompatible comparison with coverage findings even if both arms lost the same row; an explicit `not_started` row counts as an incomplete outcome, not a missing schedule entry. Reject empty, duplicate, mixed-suite or mismatched selections. Keep legacy comparisons usable as legacy-only if they lack new condition metadata. Support both existing installed suites, selecting an explicit variant when needed.
2. In `_axis_pair`, require same package revision except on `revision`, same fixture HEAD **even on `revision`** for new evidence, matching provenance and effective conditions. Compare exact dotted leaves: strategy may change only strategy fields (workflow mode or retrieval strategy); map only graph enable/map section knobs intended by that named arm; budget only named request/section caps; cache only configured cache condition. Same strategy comparison for V1/V2 uses their normal stack as a unit; use explicitly named ablations to isolate map, budget or cache changes.
3. In `_metric_values`, use the coverage flag **for the metric evaluated**, not optional-subset flags. Implement signed candidate-minus-baseline deltas for input/output/total, preflight estimate, calls, retry/expansion counts and wall times. Aggregate scheduled, correct, positive successes, expected negatives, errors, missing and paired field coverage. Per-pair efficiency requires two correct, covered arms; also aggregate all incurred positive-arm spend, including failed attempts, with an explicit missing-coverage flag. Do not turn unknown durations/retries into zeros. Enable pairing for existing retrieval rows now; Task 5 adds the expanded retrieval metrics by split and K.
4. In `load_thresholds` and `_evaluate_gate`, preserve v1 file behavior and label it legacy-only. Add a v2 policy with the same gate fields and explicit paired-regression semantics: scope `suite` or `case:<id>`, exactly matching provenance, finite nonnegative tolerance, metric-specific units and direction. For lower-is-better metrics regression is candidate minus baseline; reverse for higher-is-better. For required correctness and labeled coverage, evaluate the worst applicable pair rather than averages, and require absolute correctness/required span coverage where declared by the fixture; equally bad arms cannot pass those checks. Incompatible schedule/conditions or invalid policy is exit 2 before gate evaluation; compatible but incomplete, incorrect, failed or unevaluable requested gates exit 1; correct/no gate failure exits 0. Filter outcome exits to selected arms.

### Required Interfaces and Contracts
Never compare historical/scripted/local/live sources across provenance, nor absolutes against regression tolerances. For release gates require a paired schedule and complete coverage for applicable required fields, including failed arms; the output identifies exclusions and reasons. Zero baseline percentage is unavailable. A correctness swap at equal aggregate counts must still fail the per-case gate.

### Requirement Evidence
Original Request §§4–5, 9 and Benchmark requirements; `docs/benchmarks.md` → Comparison eligibility and Regression thresholds. Planning preflight returned `successful_pairs: 0` despite covered totals on positive arms because optional subset flags were false.

### Error Handling and Logging
DEBUG stable axis/scope/coverage counts, WARN stable incompatibility or unevaluable-gate codes, INFO eligible/pair/correctness totals.

### Tests
Extend `test_regressions.py` and `test_benchmark_cli.py` for missing optional subsets, both sides losing the same scheduled row, explicit not-started rows, proven no-send versus absent evidence, derived versus contradictory totals, mismatched provenance/revision/effective conditions, false-positive nested-pack axis matching, correctness swaps, retrieval-only comparisons, old-file reporting, unchanged v1 policy behavior and v2 exit codes.

### Acceptance Criteria
Covered totals are eligible without optional subsets; invalid comparisons cannot yield a green result; incorrect and incomplete pairs remain in the correctness denominator.

### Verification
`uv run --locked pytest tests/benchmarks/test_regressions.py tests/benchmarks/test_benchmark_cli.py` passes. A fresh paired checkout run compares successfully when untuned and shows covered positive scripted counts without claiming live savings.

## Phase Risks and Mitigations

- More metadata can make evidence brittle or sensitive: store bounded structured values, normalize relocatable paths, keep path/config source out, and test old payloads.
- Stronger release checks can expose gaps in old evidence: keep reports readable but do not silently qualify old data.

## Phase Completion Checklist

- Tasks 1–3 verified; update only the matching checkboxes in `index.md`.
- Retain the Phase 1 smoke evidence. Freeze the optimization baseline only after the Phase 2 validation and measurement fixes.
