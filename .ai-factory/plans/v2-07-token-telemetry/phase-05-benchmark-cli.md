# Phase 5: Benchmark CLI, regression gates and rollout

Plan: [index.md](index.md)  
Tasks: 9–10  
Depends on: Phase 4 / Task 8

## Objective

Make the measurement framework usable from an installed CLI and reject misleading comparisons by default.

## Current-Code Evidence

| Path | Symbols | Why it matters |
| --- | --- | --- |
| `src/lean_weave/cli/app.py`, `src/lean_weave/cli/commands/map.py` | `app.add_typer`, command group patterns | Register thin commands on the same app as both installed scripts. |
| `benchmarks/results.py`, `tests/benchmarks/test_results.py` | `compare_workflow_context`, legacy baseline/report tests | Preserve historical readers; use shared analytics and stricter evidence matching for new comparisons. |
| `docs/benchmarks.md`, `docs/context-retrieval.md`, `docs/mvp1-readiness.md` | Existing developer commands, results provenance and uncertainties | Update current documentation, not historical baseline claims. |

## Files to Change

| Path | Action | Required change |
| --- | --- | --- |
| `src/lean_weave/cli/commands/benchmark.py`, `src/lean_weave/cli/app.py` | Create, modify | Expose `run`, `compare`, `report` without changing feature entrypoints. |
| `src/lean_weave/benchmark/{compare,report}.py`, `src/lean_weave/benchmark/models.py` | Create/modify | Typed result/threshold decoding, comparison eligibility, reports. |
| `tests/benchmarks/test_benchmark_cli.py`, `tests/benchmarks/test_regressions.py` | Create | Command, comparability, unit and output coverage. |
| `docs/{benchmarks,context-retrieval,worktree-workflow,architecture}.md`, `README.md` | Modify | Commands, conditions, uncertainties, migration and public entrypoints. |

## Task 9: Add benchmark commands and threshold gates

### Intent

Make repeatable input, explicit comparison scope and portable output part of the public contract.

### Implementation Steps

1. Register `wl benchmark run --suite workflow|retrieval|all` (default `workflow`) with repeatable `--case`/`--variant`, `--repeats` (1–10, default 1), required `--output FILE` and Phase 4 opt-in live flags. Defaults are pinned suites and the variants defined in Phases 3/4. In `all`, reject case/variant filters to keep suite resolution unambiguous. Live requires `workflow`; `compare` and `report` never contact the provider even when reading live evidence. For `run` and `report --output`, write and fsync a restrictive, exclusively created temporary sibling, then publish by same-directory hard link without replacement. An existing target (including a symlink or one created after preflight) must fail without being changed. Remove only the temporary file on failure; never fall back to `replace`. Reject an already occupied output before benchmark execution, but keep the race-safe publication step.
2. Implement `wl benchmark compare BASELINE CANDIDATE --axis strategy|map|budget|cache|revision [--thresholds FILE] [--json]` and `wl benchmark report RESULT [--output FILE]` (Markdown to stdout by default). A run result can also yield internal named-variant pairs: expose `--baseline-variant` and `--candidate-variant` on `compare`; both arguments may name the same result. Require explicit variants when either input has more than one relevant variant. `report` renders observed outcomes and, for defaults in one run, paired V1/V2 deltas when eligible. `compare` and `report` share the same rendering/eligibility service, not two calculation paths. Decode only bounded regular versioned JSON files (max 16 MiB); reject unknown schema, duplicate rows and invalid/null-vs-zero data. Keep historical developer reports readable through their legacy entry point, but ineligible for new gates if evidence is missing.
3. Select one arm per variant and pair the declared schedules by `(suite, fixture_id, repetition)` before inspecting results. A schedule mismatch is incompatible, not an inner join; validate that each scheduled arm has exactly one row, including explicit failed/not-started states. Then require matching provenance, tasks, Git SHA/tree, normalized verifier/operator authority, model settings, limits, outcome contract and environment. Fingerprint canonical fixture/oracle identities and normalized settings, not per-arm temp paths or a checkpoint authority digest with absolute paths. `--axis` permits only the named condition difference: `strategy` existing V1/V2 defaults, `map` graph/map switch, `budget` named pack caps, `cache` labeled local cache state, `revision` tool implementation revision (not fixture SHA). Different provenance, fixture SHA, pricing for a cost comparison, or uncontrolled condition drift => incomparable, with stable reason. Include incomplete/failed arms in each all-scheduled correctness denominator; separate expected-negative correctness. Successful-pair token deltas require both arms correct and fully covered for that field. The all-positive-arm amortized metric in Task 7 includes incurred costs from failed features and is unevaluable if any scheduled arm is unobserved, required token evidence is incomplete, or the verified-task denominator is zero. Never substitute the successful-pair subset for this metric. Report paired counts, metric-specific exclusions and median per-pair delta where applicable; do not compute percentage delta from zero.
4. Add benchmark-only strict JSON threshold schema with `version: 1` and a list of unique gates `{metric, provenance, scope, max_regression}`. Enumerate metric names for file/symbol P@K/R@K, selected relevant tokens, missing dependency count, full covered processed/estimated tokens per verified task, correctness/success rate, retry frequency and duration. Scope is a suite aggregate or registered case; thresholds are finite nonnegative absolute changes in metric units (rates in [0,1]); use lower-is-better for missing dependencies/tokens/retries/time and higher-is-better for quality/success/relevant tokens. Do not gate live duration by default. With no policy, show deltas but do not invent threshold criteria; retain the v2-06 strict representative local-estimate test. With policy, missing coverage is unevaluable and fails the requested gate. Exit 0 for valid, complete non-regressing gated evidence; 2 for invalid options/results or incompatible requested conditions; 1 for incorrect/incomplete run or regression/unevaluable gate. Expected-negative cases that meet their exact contract are not run failures.
5. Render stage/feature/task/model/strategy/case breakdowns, verification outcomes, retries, execution timing, confidence/coverage limitations and findings. JSON comparison stdout uses a versioned schema; Markdown escapes labels and marks mock, local estimate, live provider-reported, and unavailable fields distinctly. Write only sanitized measurements and bounded configuration identifiers.

### Required Interfaces and Contracts

- `run` is offline by default; `compare` and `report` have no provider or project side effects and never grade via an LLM. Commands work from a built wheel outside the repository. stdout is machine-safe with `--json`; diagnostics go to stderr.

### Requirement Evidence

Original Request §§10, 12–15 and acceptance 3–6; compare only within an explicit, controlled axis.

### Error Handling and Logging

DEBUG suite and pair/gate counts, WARN stable non-comparability or missing-coverage reasons; never log or export input files' raw contents.

### Tests

CLI tests exercise help, paired run→compare→report, incompatible schedule/SHA/model/verification/provenance, old schemas, missing fields, a failed arm that reduces correctness while only the affected token delta becomes unavailable, zero baseline, gates at exact bound and failures, Markdown snapshots and no-clobber publication when an output appears between preflight and publish. Build a fresh wheel (`uv build --wheel`), install in a disposable environment from resolved dependencies and smoke both aliases outside the checkout with a packaged `bug_fix` run.

### Acceptance Criteria

All requested benchmark commands are installed, and reports contain comparable, attributable evidence or an explicit reason they cannot.

### Verification

`uv run pytest tests/benchmarks/test_benchmark_cli.py tests/benchmarks/test_regressions.py` and wheel smoke described above — no live requests.

## Task 10: Document operation and measurement uncertainty

### Intent

Allow independent reproduction without encouraging savings or billing claims beyond recorded evidence.

### Implementation Steps

1. At the mandatory `/aif-docs` checkpoint update `docs/benchmarks.md` with runnable commands, fixture/version/selector matrix, unchanged V1 default and frozen resume behavior, sample threshold policy and exit semantics. Cross-link stats and workflow routing/archival from `docs/worktree-workflow.md`, provenance/relevance from `docs/context-retrieval.md`, and product boundaries from README and `docs/architecture.md`.
2. Explain provider totals versus reasoning/cache subsets, absent fields, conservative budget estimates and charges, retry/unknown accounting, which timings include what, incomplete/failed denominators and task amortization, relevance labels and small holdouts, local versus provider cache, warm/cold comparability, pricing assumptions, and uncontrolled provider latency/model/cache variance. Include an opt-in live example with placeholder pricing/key, not a live outcome. Document newly attributed journal rows and forward-reader downgrade limits. Do not rewrite historical baselines or mark roadmap milestones.
3. Run the full locked suite and offline verification of docs examples (live example help/preflight only). Verify index and cache default behavior and existing CLI outputs remain unchanged.

### Required Interfaces and Contracts

- No new dependencies, hosted observability, LLM grader or orchestrator. Never publish a theoretical savings number as a measured result.

### Requirement Evidence

Original Request §15, token-efficiency bullets and out-of-scope section.

### Error Handling and Logging

No new runtime logging; documentation uses sanitized examples and calls out estimated versus measured uncertainty.

### Tests

Run both CLI entrypoint smoke checks and full suite, plus docs commands for offline fixture, stats JSON, report and compare. Do not run live as a prerequisite.

### Acceptance Criteria

An operator can reproduce and interpret an offline comparison, and knows what a live measurement does and does not establish.

### Verification

`uv run --locked pytest` and `uv run --locked wl benchmark --help` — offline success.

## Phase Risks and Mitigations

- Comparing matching labels with differing conditions manufactures deltas; require fingerprints and explicit allowed differences.
- A wheel lacking resources can appear to work in the checkout; test installed use independently.

## Phase Completion Checklist

- Both tasks meet acceptance, wheel smoke passes and docs are verified.
- Mark Tasks 9–10 in `index.md` after verification.
