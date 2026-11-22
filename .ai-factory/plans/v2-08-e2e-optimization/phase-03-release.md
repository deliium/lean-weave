# Phase 3: Measured optimization and release validation

Plan: [index.md](index.md)  
Tasks: 7–9  
Depends on: Phases 1–2

## Objective

Produce a reproducible go/no-go assessment under the current Python driver, with performance claims limited to the evidence actually collected.

## Current-Code Evidence

| Path | Symbols / evidence | Why it matters |
|---|---|---|
| `src/lean_weave/benchmark/{live,runner}.py` | `validate_live_request`, `run_live_case`, `run_live_matrix` | Installed live matrix labels all arms `v2-live`, does not forward variants, and checks deadline between arms; legacy runner has an installed child boundary. |
| `src/lean_weave/workflow/{instructions,tool_loop,context_lifecycle}.py` | stage instruction fragments, `run_tool_loop`, `_round` | Natural boundaries for counts-only wire attribution, not extra prompt/model authority. |
| `src/lean_weave/context/{packing_models,retrieval_ranking,packer}.py`, `src/lean_weave/context_cache/service.py` | `PackMetrics`, rank scoring, `_assemble`, `_lookup` | Existing bounded knobs and hotspots. |
| `docs/{benchmarks,architecture,worktree-workflow,mvp1-readiness}.md` | V1-default protocol, reporting and readiness precedent | Update documented behavior without changing ownership or defaults. |

## Files to Change

| Path | Action | Responsibility |
|---|---|---|
| `src/lean_weave/{benchmark/live,benchmark/runner,cli/commands/benchmark}.py` | Modify | Explicit paired live schedule behind existing admission; legacy behavior preserved. |
| `tests/benchmarks/{test_live_limits,test_benchmark_cli,test_workflow_matrix}.py` | Modify | No-network tests of paired opt-in, gates, failure and diagnostic instrumentation. |
| `src/lean_weave/{context/retrieval_ranking,context/packer,workflow/context_lifecycle,context_cache/service}.py` | Conditional | At most a narrowly measured improvement at the identified seam; relevant existing tests updated with it. |
| `docs/{architecture,code-index,repository-map,context-retrieval,context-packing,context-cache,benchmarks,getting-started,worktree-workflow,cli}.md`, `README.md` | Modify where applicable | Canonical guides, migration and links. |
| `docs/v2-readiness.md`, `benchmarks/validation/v2-08-{workflow,retrieval}-thresholds.json` | Create | Readiness assessment and separate version-2 policies applied by the documented commands. |

## Task 7: Enable guarded paired live validation

### Intent
Make real token comparison possible without changing the product's runtime or implicitly authorizing provider spend.

### Implementation Steps
1. In `benchmark.py::benchmark_run`, accept exactly `--variant v1 --variant v2` (either ordering) as the paired live opt-in in addition to the existing unfiltered live command; reject any other filtered live selection before admission. Default offline behavior and unfiltered single-arm live behavior remain supported, with the reported mode taken from the effective config rather than a misleading V2 label.
2. In `live.py::validate_live_request`, snapshot each permitted external case config once under private admission storage; for paired mode create two snapshots differing only in `context.workflow_mode` (`v1` and `v2-with-v1-fallback`), validate both with existing strict parser and exact case authority checks, and run only those immutable snapshots. A comparison row's normalized `context_mode` label is not a YAML key. Reuse existing allowed live cases, endpoint and environment restrictions. Compute worst-case token and cost reservation over **all** scheduled arms and repetitions before launch from the verified effective configs. Guard against non-finite/invalid limits. No extra model API, enabler by key presence, or wider workflow/tool rights.
3. In `runner.py::run_live_matrix`, reuse the installed child boundary from `live.run_live_case` with a private sanitized child environment and isolated origin. Set one absolute monotonic deadline for the admitted schedule; recompute remaining time immediately before child launch and before each external oracle, use the smaller of remaining time and the normal operation timeout, and never round up to the legacy one-second floor. Reap a timed-out child before cleanup; allow only local evidence collection/cleanup after expiry, with no further verification subprocess or send. Alternate V1/V2 order by repetition. Collect Task 1–2 evidence before cleanup from the checkpoint and journal even after child failure when a feature route is available; otherwise record a coverage gap. Retain all remaining rows as `not_started` after timeout, unknown billable outcome, incomplete required usage or incorrect result. Check strict oracles and actual strategy observations; same approvals and settings except mode.
4. Do not run live unless an operator separately approves the exact budget/deadline/config and provides credentials via environment. Store actual live artifacts only from that run. Otherwise report `not_run` and give the exact procedure in Task 9; no invented thresholds or API-savings claim.

### Required Interfaces and Contracts
Existing gateway and orchestration only. No live scenario beyond current safe positive fixture allowlist. Keep user-facing live limits and output publish semantics. Cap reservation is a conservative admission estimate, not a billing guarantee; ambiguous send cannot be retried by the harness.

### Requirement Evidence
Original Request §§4, 9–10 and Benchmark requirements; `docs/benchmarks.md` → Live DeepSeek and V2 comparison protocol.

### Error Handling and Logging
DEBUG case/arm, effective mode, remaining elapsed allowance, no credentials, raw child output or config. WARN stable reason on refused admission or halted matrix. Preserve failures even when the child didn't publish a terminal result.

### Tests
In `test_live_limits.py` and `test_benchmark_cli.py`, mock the installed launcher and assert paired budget doubling and refusal-before-launch, end-to-end strict loading of snapshots with exactly one allowed config difference, matched alternation, fixed limits and provenance, a sub-second remaining allowance, timeout/partial coverage stop, unknown send and preserved not-started arms. No test reaches DeepSeek.

### Acceptance Criteria
An explicitly authorized run can produce comparable observed input/output/total metrics for both modes; without authorization no call or measured claim occurs.

### Verification
`uv run --locked pytest tests/benchmarks/test_live_limits.py tests/benchmarks/test_benchmark_cli.py` passes without network.

## Task 8: Tune only reproducible bottlenecks

### Intent
Identify the source of V2 wins and losses, then accept only improvements that preserve correctness.

### Implementation Steps
1. Use the frozen post-Phase-2 untuned artifacts, not the planning smoke or Phase 1 artifacts. Capture like-for-like untuned attribution with the same benchmark instrumentation before changing any production setting; record both instrumentation and source tree identity. Over the dev corpus and paired scripted runs, attribute estimated input to approved source, map, stage instructions, tool schemas and accumulated tool messages using the same estimator on transient request components. Component estimates are explanatory and not assumed to sum to the complete request estimate. Keep only component counts/digests and call/stage labels in `benchmark/results.py`/`offline.py` test instrumentation; separate unsent reservations from sends. Flag repeated instructions/maps/tool outputs by count/digest without storing text. Correlate missing context, fallback and expansion with blocked/repair attempts; do not infer causality from timing alone.
2. Use one-axis controls in priority order: map section off/graph-off separately, `v2-tight` budget, then any ranked-weight change in `retrieval_ranking.py` or `retriever.py` supported by dev relevance evidence; evaluate one bounded expansion policy change only when repeated unusable expansion is observed, using existing task-scope controls. Profile cache disabled/cold/warm with Task 6 before considering bookkeeping changes in `context_cache/service.py`; invalidation correctness is mandatory regardless of speed. No mandatory changes to these knobs.
3. Pre-register metric per experiment; run at least five paired local repetitions for timing and three paired offline workflow repetitions. Select at most one candidate using dev metrics only: intended metric reproducibly improves (same direction in at least four of five timing pairs; for deterministic counts, repeat equal observations), with no required dev/workflow/safety/verification regression. Freeze its identity before opening or running the final paired holdout. On holdout required symbol/dependency regression, keep the untuned baseline; do not select a runner-up or retune against that holdout. Report cases where candidate/V2 is worse, including material latency, and revert only experiment edits made for this milestone when they fail. Do not use live for tuning. Keep current defaults if no candidate qualifies.
4. For any selected edit, compare untuned and tuned on `revision` with exactly the same fixture revision/mode/settings; strategy results compare V1/V2 at the selected package revision. Diff production changes for unrelated edits; verify approvals, caps and freshness checks were not weakened.

### Required Interfaces and Contracts
Local estimates and timings are descriptive and never substitute for live totals. No second metrics store, new strategy default, reduced verification, or cache authority. Task outcomes must include a valid “no change” result when data does not justify tuning.

### Requirement Evidence
Original Request §§7–10 and Constraints; planning smoke and cache profile in `index.md`; Tasks 4–6 establish the offline gates and baseline. Task 7 is not a tuning prerequisite.

### Error Handling and Logging
DEBUG axis/candidate IDs and measured counters only, WARN failed or unevaluable candidate gates, INFO accepted/rejected/no-change decision. Never serialize transient text or credentials.

### Tests
In `test_workflow_matrix.py` prove counts-only output and label propagation; for any selected policy add a focused regression at its existing suite, plus affected case and hidden/holdout verification without modifying labels.

### Acceptance Criteria
Any production change is attributable to a reproduced bottleneck with saved before/after data and unchanged required outcomes. Worse, unchanged and inconclusive cases are recorded.

### Verification
`uv run --locked pytest tests/benchmarks tests/context tests/context_cache tests/workflow` passes; repeat saved paired commands from Task 9 and compare against the frozen untuned artifacts.

## Task 9: Validate release and document rollout

### Intent
Ship reproducible operating instructions and a readiness decision, not merely a list of passing tests.

### Implementation Steps
1. Prepare a fresh private evidence directory outside the checkout. Capture verified source revision, wheel hash and source/lock binding for installed runs, fixture hashes, tool versions, command and exit status, run IDs, checksums, raw bounded JSON, comparisons and profiler summaries. Keep operator inputs and secret material out. Write separate v2 workflow (`offline_scripted`) and retrieval (`local_estimate`) policies at the paths above, with zero allowable regression in required correctness and labeled coverage respectively; use exact-case tests for safety and per-case required span checks rather than averages. Evidence remains linked and independently rerunnable; use aggregate redacted summaries in docs.
2. Run the full locked test suite, workflow paired matrix at three repetitions, retrieval strategy and map/budget/cache ablations with frozen labels, cache workload with sample windows, and (only if authorized) paired live matrix under Task 7. Verify any claimed savings against coverage and correctness before publishing. Negative controls must fail as designed; include incurred tokens from failed attempts and identify incomplete arms.
3. Update the named guides through the mandatory docs checkpoint, recording an explicit reason for each listed page left unchanged: describe V2 architecture and entry points, index configuration and explicit update, map usage, ranking and packing budgets, cache isolation/staleness/recovery, benchmarks and troubleshooting. Record `v1` default, `v2` strict and `v2-with-v1-fallback` opt-in, fallback limits, resume same-mode/authority requirement, and starting a new V1 workflow as rollback rather than rewriting a checkpoint. Clarify bounded summaries in `ControlRecord`, transient adapter internals, and old reader limitations for attributed journals.
4. In `docs/v2-readiness.md`, include implementation summary, V1/V2 per-mode token table, retrieval scores/misses, verification/operational results, cache effectiveness, regressions, limitations, remaining technical risks and future recommendations; cite the exact evidence inventory, coverage and worse cases. Verdicts: **not ready** if any required safety, correctness, coverage or reproducibility gate fails; **ready for opt-in, live savings unvalidated** if those gates pass but live is not run/incomplete; **ready for opt-in, live measured** if gates pass and eligible live evidence is complete, whether it shows savings or not. A live correctness/safety failure is a release blocker, not an evidence gap. Keep V1 default in all cases.

### Required Interfaces and Contracts
Thresholds do not redefine verification. Preserve raw metrics/provenance, package commands, checkpoints, approvals and production driver. No commit/push or release publication implied by the report.

### Requirement Evidence
Original Request §§9–12, Required report, Benchmark requirements and all ten acceptance criteria; `docs/mvp1-readiness.md` is precedent, not comparison evidence.

### Error Handling and Logging
INFO evidence revision/counts and verdict; WARN blocked/incomplete gates and operator next action. Never claim savings from estimates, scripted equal outputs, missing provider usage, or cross-provenance comparisons.

### Tests
Extend CLI docs-command smoke and readiness gate coverage in `test_benchmark_cli.py` for published commands, inaccessible live path, and accurate failure propagation; validate internal documentation links and approve no change to existing entry points with installed-wheel smoke.

### Acceptance Criteria
Another operator can reproduce offline evidence and interpret a documented opt-in go/no-go with each remaining risk and known regression visible.

### Verification
From an approved private `$E` directory that already exists and has unused filenames:

```sh
uv run --locked pytest
uv run --locked wl benchmark run --suite workflow --variant v1 --variant v2 --repeats 3 --output "$E/workflow.json"
uv run --locked wl benchmark compare "$E/workflow.json" "$E/workflow.json" --axis strategy --baseline-variant v1 --candidate-variant v2 \
  --thresholds benchmarks/validation/v2-08-workflow-thresholds.json --json
uv run --locked wl benchmark run --suite retrieval --variant v1_default --variant v2_default --output "$E/retrieval.json"
uv run --locked wl benchmark compare "$E/retrieval.json" "$E/retrieval.json" --axis strategy --baseline-variant v1_default --candidate-variant v2_default \
  --thresholds benchmarks/validation/v2-08-retrieval-thresholds.json --json
uv run --locked python -m benchmarks.context_cache --json > "$E/cache.json"
uv run --locked wl benchmark report "$E/workflow.json" --output "$E/workflow.md"
```

For retrieval ablations rerun the retrieval command with the exact variant pairs in Task 5 and a fresh output path per pair; compare each with `--axis map`, `--axis budget`, or `--axis cache` as applicable (`map` covers the separately controlled graph experiment). Keep exploratory differences separate from release strategy gates; preserve commands, exact policy files and nonzero exits. Expect complete offline evidence and no required regressions, not a mandatory performance gain.

Only after separate authorization, with the approved operator settings in `$LIVE_CONFIG_DIR`, `$PRICING` and the limit variables below, run:

```sh
uv run --locked wl benchmark run --live --suite workflow --case bug_fix --case multi_file_feature \
  --variant v1 --variant v2 --repeats "$REPEATS" --live-config-dir "$LIVE_CONFIG_DIR" \
  --max-tokens "$MAX_TOKENS" --max-cost-usd "$MAX_COST_USD" --pricing "$PRICING" \
  --deadline-s "$DEADLINE_S" --output "$E/live.json"
uv run --locked wl benchmark compare "$E/live.json" "$E/live.json" --axis strategy \
  --baseline-variant v1 --candidate-variant v2 --json
```

## Phase Risks and Mitigations

- Live sample size and remote variability limit inference; report the actual covered schedule and uncertainty.
- A timing win can hide correctness costs; do not merge it unless required oracles, gates and operational invariants hold.

## Phase Completion Checklist

- Tasks 7–9 verified and `index.md` updated; live authorization/evidence explicitly recorded.
- Reproduction instructions, gate results and readiness verdict are independently reviewable.
