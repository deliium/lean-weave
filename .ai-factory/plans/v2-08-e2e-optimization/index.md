<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: V2 End-to-End Optimization and Release Validation

Mode: ultra  
Plan ID: `v2-08-e2e-optimization`  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-23

## Original Request

Plan ID: v2-08-e2e-optimization
Plan Title: V2 End-to-End Optimization and Release Validation
Milestone: V2 - Context Intelligence

Dependencies:
- v2-01-code-indexer
- v2-02-repository-map
- v2-03-context-ranking
- v2-04-context-budget
- v2-05-context-cache
- v2-06-workflow-integration
- v2-07-token-telemetry

Goal:
Validate the complete V2 Context Engine and measure its effect on total processed LLM tokens, implementation correctness, and workflow reliability.

Authoritative architecture:
- Minimal deterministic Python state machine.
- Existing WorkflowOrchestrator.
- Existing DeepSeek Gateway.
- Existing StateStore and worktree isolation.
- No mandatory LangGraph dependency.
- v3-01-langgraph-orchestrator remains deferred.

Before planning:
1. Inspect all implemented V2 components.
2. Review V1 benchmark baselines.
3. Review existing integration and regression tests.
4. Identify actual performance bottlenecks.
5. Avoid unrelated architecture changes.

Functional requirements:

1. Validate the complete V2 pipeline:
   - source indexing
   - repository mapping
   - context retrieval
   - context ranking
   - context packing
   - context caching
   - workflow integration
   - token telemetry

2. Run end-to-end scenarios:
   - simple bug fix
   - single-file feature
   - multi-file feature
   - refactoring
   - test generation
   - verification failure
   - repair and retry
   - context expansion
   - interrupted execution
   - resumed execution
   - source modifications
   - cache invalidation

3. Validate backward compatibility:
   - existing wl commands
   - Python state machine
   - FeatureState
   - StateStore
   - review mode
   - automatic mode
   - Git worktrees
   - token budgets
   - approval boundaries
   - failure reporting

4. Compare V1 and V2 on identical tasks.

Measure:
- total input tokens
- total output tokens
- total processed tokens
- LLM call count
- retry count
- context expansion count
- execution duration
- successful implementations
- verification outcomes

5. Compare retrieval quality:
   - Recall@K
   - Precision@K
   - missing required symbols
   - context pack size
   - duplicate content avoided

6. Evaluate caching:
   - repeated indexing
   - unchanged repositories
   - changed source files
   - dependency invalidation
   - worktree isolation
   - cache recovery

7. Identify excessive token consumption:
   - irrelevant source fragments
   - repeated instructions
   - redundant repository maps
   - unnecessary context expansion
   - repeated tool outputs
   - failed attempts caused by missing context

8. Tune only based on measurement:
   - retrieval ranking weights
   - context pack budgets
   - repository map selection
   - expansion policies
   - cache invalidation behavior

9. Validate correctness:
- Do not accept token improvements that cause meaningful correctness regressions.
- Do not weaken verification to improve benchmarks.
- Do not hide failures from aggregate metrics.
- Document cases where V2 performs worse than V1.

10. Validate operational safety:
- No unintended workspace modifications.
- No unauthorized tool execution.
- No repeated uncertain side effects after resume.
- No leakage between worktrees.
- No unbounded LLM loops.
- No unapproved file access.

11. Update documentation:
- V2 architecture
- Index configuration
- Repository map usage
- Context ranking
- Token budgets
- Cache behavior
- Benchmark commands
- Migration from V1
- Troubleshooting

12. Produce a V2 readiness report.

Required report:
1. Implementation summary
2. V1 versus V2 token comparison
3. Retrieval quality results
4. Workflow reliability results
5. Cache effectiveness
6. Correctness regressions
7. Known limitations
8. Remaining technical risks
9. Recommendations for future milestones

Benchmark requirements:
- Same initial Git revisions.
- Comparable models and execution settings.
- Identical verification conditions.
- Include failed attempts.
- Preserve raw metrics.
- Distinguish measured results from estimates.
- Do not invent performance improvements.

Constraints:
- No LangGraph migration.
- No new orchestrator.
- No MCP or ACP.
- No background workers.
- No distributed execution.
- No multi-agent architecture.
- No mandatory embeddings.
- No unrelated refactoring.

Acceptance criteria:
1. V2 Context Engine works end-to-end.
2. Existing V1 functionality remains operational.
3. Python orchestrator remains the production driver.
4. Index and cache invalidation are correct.
5. Context selection is reproducible.
6. Token usage is measured consistently.
7. V1 and V2 comparisons are supported by reproducible evidence.
8. Any regressions are documented.
9. Documentation is complete.
10. V2 readiness is explicitly assessed.

Save using the exact plan ID:
v2-08-e2e-optimization

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes
- Live: not authorized by this planning request; execution requires separate explicit operator approval, config, rates, limits and credentials.

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: Validate release readiness for the implemented V2 stack without reopening the roadmap's completed delivery milestone or absorbing general hardening.

## Requirements Reconciliation

Authority: Original Request and its authoritative architecture govern this plan; `.ai-factory/RULES.md` constrains implementation. The dependency bundles and actual code refine the operating contracts; historical evidence and the roadmap do not override them.

| Decision / supported combination | Sources and relationship | Verification |
|---|---|---|
| V1 default and opt-in V2 strategy pair, both auto: same fixture pin, budgets, verifier, approvals, package revision; isolated checkpoints and exact expected outcomes | Original Request §§1–4; `.ai-factory/ARCHITECTURE.md` → Boundaries; `docs/benchmarks.md` → V2 comparison protocol agree | Installed paired matrix on actual packaged fixtures, including `multi_file_feature` |
| Strict V2 vs V2 with fallback: only permitted no-complete-source outcome can use bounded V1 rendering; stale/denied refusal cannot widen scope or dispatch | Original Request §§3, 9–10; `workflow/context_lifecycle.py` and `config/models.py` refine the supported modes | Strict refusal, fallback guarded by actual wire/output/read assertions, no orphan effect |
| Review vs auto; valid/stale approval; safe/uncertain resume | Original Request §§2–3, 10; `docs/worktree-workflow.md`, `workflow/{session,state,lifecycle}.py` refine transitions | No write/check before approval; changes refuse; safe continuation retains spend, uncertain effects never replay; worktree state stays isolated |
| Checked/missing/stale index; cache off/cold/warm/changed/corrupt; graph and packing independent of authority | Original Request §§1, 5–6, 10; `docs/{code-index,repository-map,context-cache,context-packing}.md` agree | Observe the actual scoped fact source; compare incremental vs clean results, typed refusal or uncached output, and per-checkout namespace |
| Scripted vs local estimate vs measured live vs historical: compatible provenance only; failures and unknowns retained | Original Request §§4, 7, 9 and Benchmark requirements; `docs/benchmarks.md` → eligibility and uncertainty agree; current comparison does not fully enforce this | Declared schedule, verified source/build identity, field coverage, separate versioned gates and no live unless authorized |
| Telemetry migration: bounded checkpoint summaries exist; raw adapter details remain transient | `workflow/state.py` and `docs/benchmarks.md` refine the older `.ai-factory/DESCRIPTION.md` “non-persistent telemetry” description | Project `wl stats` from journal/checkpoint, test old checkpoints and prevent duplicate ledger; clarify human docs |

## Architecture and Decisions

- Keep `lean_weave:main`, `WorkflowOrchestrator`, `DeepSeekProvider`, StateStore and managed worktrees. No LangGraph dependency (`v3-01-langgraph-orchestrator` deferred), second orchestrator, MCP/ACP, worker, distributed execution, multi-agent runtime or mandatory embeddings. Do not change default mode, authority, state schema or verification checks to win benchmarks.
- V2 dependency bundles under `.ai-factory/plans/`: `v2-01-code-indexer` (checked facts), `v2-02-repository-map` (graph), `v2-03-context-ranking` (rank), `v2-04-context-budget` (pack/wire bounds), `v2-05-context-cache` (disposable metadata), `v2-06-workflow-integration` (stage routing), `v2-07-token-telemetry` (journal/report/benchmark). All corresponding implementation surfaces exist at the inspected revision.
- Benchmark by mechanism: normal strategy pair (V1 vs opt-in V2 default), controlled graph/map/budget/cache experiments, strict-mode/fallback operational controls, and a separate live-positive schedule only with approval. Use same check conditions, preserve all scheduled arms, and distinguish host/duration noise from deterministic work.
- Holdout and independent oracles gate correctness. A locally cheaper pack with a missing required symbol/dependency or a verification regression is not a win. No threshold implies higher readiness merely from fewer tokens.

### Planning reconnaissance (not release evidence)

- Historical V1 baseline `benchmarks/baselines/v1-offline-baseline.json` and summary: seven scripted outcomes at `a7dca8722b3e4b0f2d79ca8324f972bc5d0bbfb5`; its supposed repair case did not retry, live not run. Use for fixture continuity only.
- On `dcde3b42cd8294541e19e9ec6d5c06ba358bf0d2`, one offline `wl benchmark run --suite workflow --variant v1 --variant v2` gave 14/14 correct arms, including one workflow repair in each repair arm. Positive scripted totals matched (266 per variant); local preflight estimates for `multi_file_feature` were 5,075 vs 4,449. The comparator still returned zero eligible total-token pairs because unrelated optional counters were uncovered. No live requests were made; scripted equality and local estimates do not measure DeepSeek token savings.
- A separate `cProfile` run of `benchmarks.context_cache` on two tiny packaged cases observed 128 retrieval executions for 64 retrieve+pack cycles. Cache identity and lookups were substantial cumulative costs; warm skipped counted ranking/span work but was not consistently faster than disabled (e.g. indexed `multi_file_banner`: about 23 vs 13 ms median under profiling). These are workload-specific observations, not production timing. Repeated scoped scans, index row replacement, ranking fan-in and repeated pack serialization are hypotheses to measure, not mandates to optimize.
- Existing CLI/workflow tests cover many individual controls, but the V2 “matches V1” CLI test runs only V2, stage attribution is not an HTTP body field, and the installed live runner is not paired. The phases close those evidence gaps before release decisions.

## Phase Index

1. [Phase 1: Comparable evidence](phase-01-evidence.md) — Tasks 1–3
2. [Phase 2: Workflow and context validation](phase-02-validation.md) — Tasks 4–6
3. [Phase 3: Measured optimization and release validation](phase-03-release.md) — Tasks 7–9

## Cross-Phase Dependencies

- Task 2 depends on 1 (conditioned arms); Task 3 depends on 1 and 2 (stable inputs and raw observations).
- Task 4 depends on 1–3 (production-like arms and strict evidence); Task 5 depends on 3 (paired comparison); Task 6 depends on 4 and 5 (scoped workflow and ranking/pack probes).
- Task 7 depends on 1–3 (shared conditions, metrics and pairing) and is sequenced after Phase 2; Task 8 depends on 4–6 (frozen offline baseline and scenario gates), not on Task 7. Task 9 depends on 7 and 8 (live disposition and frozen results).

## Tasks

### Phase 1: Comparable evidence

- [x] Task 1: Pin benchmark conditions ([details](phase-01-evidence.md#task-1-pin-benchmark-conditions))
- [x] Task 2: Preserve bounded attempt evidence ([details](phase-01-evidence.md#task-2-preserve-bounded-attempt-evidence)) (depends on 1)
- [x] Task 3: Enforce paired comparison eligibility ([details](phase-01-evidence.md#task-3-enforce-paired-comparison-eligibility)) (depends on 1, 2)

### Phase 2: Workflow and context validation

- [x] Task 4: Validate paired workflow scenarios ([details](phase-02-validation.md#task-4-validate-paired-workflow-scenarios)) (depends on 1, 2, 3)
- [x] Task 5: Measure retrieval and packing quality ([details](phase-02-validation.md#task-5-measure-retrieval-and-packing-quality)) (depends on 3)
- [x] Task 6: Validate cache invalidation and profile overhead ([details](phase-02-validation.md#task-6-validate-cache-invalidation-and-profile-overhead)) (depends on 4, 5)

### Phase 3: Measured optimization and release validation

- [x] Task 7: Enable guarded paired live validation ([details](phase-03-release.md#task-7-enable-guarded-paired-live-validation)) (depends on 1, 2, 3)
- [x] Task 8: Tune only reproducible bottlenecks ([details](phase-03-release.md#task-8-tune-only-reproducible-bottlenecks)) (depends on 4, 5, 6)
- [x] Task 9: Validate release and document rollout ([details](phase-03-release.md#task-9-validate-release-and-document-rollout)) (depends on 7, 8)

## Commit Plan

- **Commit 1** (after Tasks 1–3): `fix(benchmark): enforce comparable release evidence`
- **Commit 2** (after Tasks 4–6): `test(context): validate v2 workflow and cache parity`
- **Commit 3** (after Tasks 7–9): `feat(benchmark): validate v2 readiness with guarded comparison`

## Definition of Done

- Complete V2 path observed with scoped approved inputs, verified fresh revisions, isolation and deterministic selection. V1 CLI, state, auto/review, recovery, approvals and verification remain operational under the production Python driver.
- Reproducible paired workflow, retrieval and cache evidence includes per-arm provenance, uncertainty and failures; no verification, scope, budget or no-replay gate is weakened.
- Tuned defaults change only if a controlled comparison proves benefit without required regression; explicitly report worse or inconclusive cases.
- Usage labels distinguish estimates from live counters; approval-dependent evidence may remain unavailable, but cannot support a claim about measured savings.
- Complete operator documentation and `docs/v2-readiness.md` with a clear opt-in verdict, reproducible evidence, limitations, risks and future recommendations.
