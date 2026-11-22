# Phase 2: Workflow and context validation

Plan: [index.md](index.md)  
Tasks: 4–6  
Depends on: Phase 1

## Objective

Exercise the real checkout, index, graph, retrieval, pack, cache and workflow boundaries with matched workload inputs and explicit oracles. Establish a measured baseline and prove cache transparency before considering speedups.

## Current-Code Evidence

| Path | Symbols / evidence | Why it matters |
|---|---|---|
| `src/lean_weave/workflow/{session,context_lifecycle}.py` | `FeatureSession`, `WorkflowContextAdapter`, `_scan_scope`, `_round` | V2 uses scoped L0/L1 and bounded expansion; the scan-to-pack handoff is a separate freshness boundary. |
| `src/lean_weave/code_index/index.py`, `src/lean_weave/repository_map/{service,graph}.py` | `_reconcile`, `read_generation_for_retrieval_scan`, `graph_for_retrieval_scan`, `scoped_graph_for_scan`, `refresh_graph` | Admitted index facts, generation checks, targeted dependency invalidation and workflow graph scope. |
| `src/lean_weave/context/{retriever,packer,packing_gateway}.py` | `_execute`, `_rank_local`, `_compose`, `_assemble`, `prepare_packed_request` | Rank/pack scans and guarded request composition are independently observable. |
| `src/lean_weave/context_cache/{service,retrieval,models}.py` | `_location`, `_lookup`, `RetrievalCache.proof`, `_bounded` | SQLite and identity checks cost time; oversize proofs must degrade without changing output. |
| `src/lean_weave/benchmark/retrieval.py`, `benchmarks/context_cache.py` | `evaluate_retrieval`, `_v2_spans`, `_run_mode`, `_instrument` | Existing labeled corpus and local cache workload; separate warm-up from measured operations. |

## Files to Change

| Path | Action | Responsibility |
|---|---|---|
| `src/lean_weave/benchmark/{fixtures,offline,runner,retrieval}.py`, `src/lean_weave/benchmark/fixtures/` | Modify/add fixture files | Paired scripts and reproducible retrieval evidence. |
| `tests/cli/test_feature_{context,recovery,isolation,cache}_e2e.py`, `tests/workflow/test_context_expansion.py` | Modify | Lifecycle, guard and isolation assertions in both applicable modes. |
| `tests/benchmarks/test_{workflow_matrix,retrieval_pack,context_cache}.py`, `benchmarks/context_cache.py` | Modify | Workload evidence, relevance and cache profiling. |
| `tests/code_index/test_incremental.py`, `tests/repository_map/test_incremental.py`, `tests/context/test_retriever.py`, `tests/context_cache/test_retrieval.py` | Modify | Revision, edge provenance and bounded-cache regression probes. |
| `src/lean_weave/{workflow/context_lifecycle,context/retriever,context_cache/retrieval}.py` | Conditional narrow fix | Only a reproduced handoff, direction or proof failure; no speculative changes. |

## Task 4: Validate paired workflow scenarios

### Intent
Validate correctness, authority, and resume with real CLI/session/state/worktree and injected offline transport, without relying on the model to choose a passing patch.

### Implementation Steps
1. Keep the seven existing pinned manifests. Add `refactor_preserve_behavior` and `test_generation` to the installed corpus via `fixtures.py`: the first extracts a helper while preserving two observable cases and the public callable, the second adds assertions covering both a normal and edge case without editing production files. For each add `trees/<id>/`, `manifests/<id>.json`, `edits/<id>.json` and `oracles/assert_<id>.py`; use trusted external data-only AST/token checks and wrong/unchanged counterexamples, not imports of generated workspace code. Pin each HEAD using existing deterministic Git materialization; do not loosen existing manifests or checks.
2. Use `runner.py` and its existing `FeatureSession` for paired auto cases. Expand CLI tests using the installed entrypoint smoke and injected HTTP transport for review pause/approve, wrong/stale approval denial, generic failed verification without repair, bounded repair/exhaustion and safe interruption/resume. Run existing status/result/cancel, approval/config drift, unavailable isolation backend, dirty origin, separate checkout and uncertain-effect scenarios under both `v1` and `v2-with-v1-fallback` where applicable, retaining their exact assertions.
3. Add a cache-enabled V2 worktree scenario over the real `multi_file_feature`: start in review, explicitly build the index in the paused managed worktree using the same effective policy, approve, and observe scoped graph, rank, pack, admitted gateway request and `wl stats`. Instrument `WorkflowContextAdapter._facts_for` and `CodeIndex.scoped_facts` in the test to distinguish a successful indexed read from cache reuse or local parsing; assert a cold indexed read, then safe reuse, without relying on successful output alone. In the same fixture verify the full checked repository map using the separate map entrypoint, and repeat offline in a second same-HEAD checkout with unchanged approval conditions and distinct namespace. V1 is the workflow negative control for V2-specific observations. Never implicitly build an index in feature execution.
4. Extend `test_context_expansion.py`: naturally omitted current-task source followed by supported read delivers one bounded V2 expansion, duplicate read does not spend expansion twice; out-of-scope read is denied using benign fixtures, owned patch shows fresh source without resetting limits. Inject a source/import edit and a raw-only LF↔CRLF edit at `_scan_scope` → packing boundary; require current coherent graph/source/raw revision or typed refusal before reservation or transport. If reproduced, require scoped revisions from the initial scan to match canonical **and raw** revisions before reusing a graph or editable set; fail stale rather than bypassing the guard.
5. Assert after each run: origin status/bytes and sibling sentinel unchanged; managed worktree diff exactly within approved paths; no checks before review approval, no unauthorized execution; effects marked uncertain are not replayed; known saved continuations use the same feature ID and retained budget/journal; terminal resume is idempotent. Retain explicit outcome rows or test IDs in release evidence.
6. Add strict `v2` versus `v2-with-v1-fallback` coverage for no complete approved source unit: require refusal before dispatch versus one properly scoped V1-rendered attempt validated by the same oracle. Check actual HTTP output cap when packing cap is lower than provider config, a benign denied-read canary in the prompt, and no fallback on stale/denied/budget/guard/retry failure. Compare packed transport retries separately from workflow retries. Assert stage from the journal/request boundary, not the HTTP JSON, and make one patch script consume the actual admitted raw revision rather than fixture-side guessed state. Use a deterministic transport barrier for cancellation and assert no new effects after cancellation; exercise saved resume with changed context mode, checkout HEAD or authority and require no send. Exercise managed and clean adopted checkout config provenance. For any reproduced scope/cap/provenance issue, fix at existing `workflow/{context_lifecycle,orchestrator,tool_loop,session}.py` admission/request binding seam, or block release; no widening budgets, context or execution.

### Required Interfaces and Contracts
Production entry points and config flags remain unchanged; preserve FeatureState/checkpoint format, mode/config fingerprints, plan approval gates and `IsolationBackend`. Git worktrees are not a process sandbox. Context/index/cache never grant authority. V2 fallback is visible and cannot count as proof of positive V2 coverage. Assert status, reason and effect counts, not just CLI exit; CLI can return zero for a rendered failure.

### Requirement Evidence
Original Request §§1–3, 9–10; `.ai-factory/RULES.md`; `docs/worktree-workflow.md`; packaged `multi_file_feature` and `verify_then_repair`.

### Error Handling and Logging
DEBUG sanitized mode, stage, observation counts; WARN typed refusal or recovery reason, no source or credentials. On mismatch preserve the failing test row and don't rerun with wider approval.

### Tests
Use the named CLI suites, `test_workflow_matrix.py`, `test_context_expansion.py`, and `tests/providers/test_deepseek_packed_context.py`; assert both successful and no-send/no-effect states. Validate counterexamples for both new oracles and installed-wheel fixture availability.

### Acceptance Criteria
All requested workflow scenarios are evidenced by specific paired rows or regression tests, including resumed work without replay and a positive indexed/packed/telemetry V2 path.

### Verification
`uv run --locked pytest tests/benchmarks/test_fixtures.py tests/benchmarks/test_workflow_matrix.py tests/cli/test_feature_{offline,recovery,isolation,context,cache}_e2e.py tests/workflow/test_context_expansion.py tests/providers/test_deepseek_packed_context.py` passes.

## Task 5: Measure retrieval and packing quality

### Intent
Identify missing evidence and duplicate content without conflating ranking scores, packed source, and gateway usage.

### Implementation Steps
1. Extend `benchmark/retrieval.py::evaluate_retrieval` using packaged `cases.json`, `K_VALUES`, existing relevance labels and `_union_spans`. Report per-case file/symbol P@K and R@K with denominators and missing required labeled symbols/dependencies by full span coverage, with `null` for not applicable. Keep dev and holdout separate. Record actual accepted pack estimated input and unique source estimate separately.
2. Use `PackMetrics.deduplicated` as V2 pack decisions count (not measured tokens); report same-revision overlapping line count as a comparable emitted-content duplicate metric from pre-union spans in both strategies. Never persist excerpts. Record run-boundary repeated fragment observations separately where available; don't infer duplicates avoided from missing telemetry.
3. Extend Task 3 comparison to consume typed retrieval metrics by split, K and case and render coverage. Compare `v1_default`/`v2_default` for strategy, `v2_default`/`v2_map_off` for map rows, `v2_map_off`/`v2_no_graph` for graph contribution (both use map budget zero), `v2_default`/`v2_tight` for budget and `v2_cache_disabled`/`v2_cache_cold`/`v2_cache_warm` in cache-only pairs. Reject mixed axes. Evaluate equivalent normalized imports and reverse importer path/range/digest in `test_retriever.py`; if `_rank_local` selects reverse edges on the importer rather than the target, fix that predicate with a minimal exact-edge regression.
4. Assert reproducibility by repeating same scope/revision/config in independently prepared scratch checkouts; compare stable ranked order, source coverage, packing sizes and digests, excluding timing and checkout identity.
5. Make this reproducibility executable through `runner.py::run_retrieval_matrix`: honor `--repeats` with Task 1's schedule, use a fresh per-arm scratch path and retain every failed/not-started entry. In `_v2_spans`, resolve the effective map cap with an explicit `None` check; confirm map-off sends no map rows while graph-only still supplies graph evidence, and no-graph performs neither graph load nor unneeded index build. Bind actual effective cache configuration to the arm label and use the same cache wiring for map/retrieval/pack as the measured product path. Separate setup/priming from measured pack time and reset rank/cache observers at the boundary; if priming does not complete, mark the warm arm degraded instead of counting it as valid warm evidence.

### Required Interfaces and Contracts
Use additive nested `retrieval` evidence, not a new product API. Estimates remain `local_estimate`. Holdout is used for final validation only, not weight or budget selection. A miss counted at K and a missing actual packed symbol are separate endpoints.

### Requirement Evidence
Original Request §§5, 7–9; `docs/context-retrieval.md`; packaged `dev_auth` and `holdout_billing` cases.

### Error Handling and Logging
DEBUG stable case/variant IDs, K/denominator and pack outcome counts; WARN unavailable labels or typed refusal, not query/source strings.

### Tests
Extend `test_retrieval_pack.py` for full versus partial required-symbol coverage, duplicated spans, no-label/null, deterministic order and baseline versus ablated rows; with a fixed case, assert two independent rows per variant when using `--repeats 2`, explicit zero map/index behavior, measured cache counters excluding priming, and warm invalidity when priming fails. Add the reverse-edge assertion in `tests/context/test_retriever.py`.

### Acceptance Criteria
Raw scored rows, selected sizes, misses and duplicate observations are traceable and repeatable; no empty comparison passes.

### Verification
`uv run --locked pytest tests/benchmarks/test_retrieval.py tests/benchmarks/test_retrieval_pack.py tests/benchmarks/test_regressions.py tests/context/test_retriever.py` passes.

## Task 6: Validate cache invalidation and profile overhead

### Intent
Prove persistence is transparent and determine whether it is saving work or latency in each workload.

### Implementation Steps
1. Extend `benchmarks/context_cache.py::_run_mode` with measured-window deltas and every wall/CPU sample; reuse Task 5's window definitions. Prime outside measurement; compare disabled, cold, warm same/fresh process with and without a checked index. For warm fresh-process samples, share only the intended checkout-local cache/index across a fresh child process and measure operation time in the child; report parent launch-inclusive wall separately. Add separately timed index initial build, unchanged update, edit update, rename/delete/ignore updates and clean reference build; count fact extraction, source admission, graph resolution, SQLite location/lookup/transaction work and pack assemblies. Preserve output digests and per-kind hits/misses, DB bytes and zero LLM calls; do not treat cache hits as provider savings.
2. Exercise a tracked importer and its target: edit/change canonical and raw revisions, shadow/delete/rename target, or change ignore/policy. Assert unchanged updates parse/write no facts anew, incremental output agrees semantically with clean rebuild, affected graph/retrieval/pack results refresh, prebuilt stale map refuses/degrades, and unrelated facts are reused where supported. Add corrupt/missing database and clear/rebuild cases; power interruptions/epoch-stale rows never become authority. Keep evidence of old/new digests and affected work, not content.
3. Verify separate same-HEAD managed worktrees, no cache namespaces crossing absent explicit trusted peer reuse, and origin/cache isolation. Probe proof-size limit with 8,192/8,193 admitted tiny files within scan caps or an equivalent injected proof-boundary test. If `RetrievalCache.proof` rejects an oversized proof before normal bypass, make that result ineligible for persistent reuse at its construction seam and continue uncached; don't drop inputs or weaken scan caps.
4. Profile the existing tiny corpus plus a bounded scaled workload holding approved scope constant while increasing off-scope fanout (100/1,000 files) and one dense-import tree. Distinguish separate retrieve+pack invocations from repeated workflow round work. Time without the profiler for decisions; profile a separate run for attribution, save sample windows and counters, and report untuned observations before any change.

### Required Interfaces and Contracts
Keep identical selected evidence across cache states for same revision/settings; changes must match clean current computation. No DB format or trust-boundary changes by default. Existing benchmark JSON remains interpretable with additive fields; scaled inputs are synthetic and explicitly labeled.

### Requirement Evidence
Original Request §§1–2, 6, 8–10; `docs/code-index.md`, `docs/context-cache.md`; current bench profile saw 128 retrieval executions for 64 retrieve+pack cycles and warm avoided counted rank/span work while remaining slower than disabled in several tiny scenarios.

### Error Handling and Logging
DEBUG operation/size/count/timing and safe availability codes; WARN stale/corrupt/oversize evidence and route through existing fail-closed/bypass path. Never bypass security checks or emit path contents.

### Tests
Extend `test_context_cache.py`, code-index/map incremental tests, cache store/retrieval tests and CLI worktree cache tests. Cover equivalence, invalidation, recovery, size bound and isolation with isolated XDG and no provider.

### Acceptance Criteria
Every state has latency and deterministic work evidence; cache hits can be cheaper or costlier and do not change outcomes. No-op, stale and recovery semantics are explicitly verified.

### Verification
`uv run --locked pytest tests/code_index tests/repository_map tests/context_cache tests/benchmarks/test_context_cache.py tests/cli/test_feature_cache_e2e.py` and `uv run --locked python -m benchmarks.context_cache --json` pass.

## Phase Risks and Mitigations

- Scripted completions can force success despite poor relevance: use independent labeled retrieval, context-aware assertions and separate live evidence.
- Tiny workloads exaggerate bookkeeping: repeat and stratify by size; do not use profiled wall time as a release threshold.

## Phase Completion Checklist

- Tasks 4–6 verified; update only matching `index.md` checkboxes.
- Freeze untuned workflow, retrieval and cache evidence at the recorded tree identity after Tasks 1–6 and before tuning. Save commands, policy inputs, checksums and failures; holdout outputs are not inspected during candidate selection.
