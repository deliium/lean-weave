<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Token Analytics and Context Benchmark Framework

Plan ID: `v2-07-token-telemetry`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; explicitly select this bundle for implementation)  
Created: 2026-11-23

## Original Request

Plan ID: v2-07-token-telemetry
Plan Title: Token Analytics and Context Benchmark Framework
Milestone: V2 - Context Intelligence

Dependencies:
- v1-03-deepseek-gateway
- v1-08-e2e-validation
- v2-03-context-ranking
- v2-04-context-budget
- v2-06-workflow-integration

Goal:
Extend existing token telemetry to measure context retrieval quality and total LLM token efficiency across complete feature workflows.

Before planning:
1. Inspect existing MetricsCollector.
2. Inspect DeepSeek usage reporting.
3. Inspect V1 benchmark fixtures.
4. Inspect workflow result and token budget accounting.
5. Reuse current metrics storage and reporting.
6. Avoid duplicate token accounting.

Requirements:

1. Extend existing telemetry with:
   - context pack token estimates
   - selected file count
   - selected symbol count
   - repeated content avoided
   - context expansion count
   - cache hit and miss counts
   - retrieval duration
   - ranking quality
   - token budget consumption

2. Record complete workflow usage:
   - input tokens
   - output tokens
   - total processed tokens
   - reasoning tokens when reported
   - cache-hit tokens when reported
   - LLM call count
   - retry count
   - failed attempt usage
   - execution duration

3. Aggregate by:
   - feature
   - implementation task
   - workflow stage
   - model
   - context strategy
   - benchmark case

4. Preserve provider usage semantics.

Do not add cached or reasoning token counts twice
when they are already included in provider totals.

5. Compare:
   - V1 context selection
   - V2 context selection
   - repository map configurations
   - different context budgets
   - caching enabled and disabled

6. Build reproducible retrieval evaluation fixtures.

Include:
- known relevant files
- known relevant symbols
- expected source locations
- different task categories
- multiple repository structures

7. Evaluate retrieval using:
   - Precision@K
   - Recall@K
   - relevant tokens per context pack
   - missing required dependency count

8. Evaluate full workflows using:
   - total tokens per attempt
   - total tokens per successful task
   - implementation success rate
   - retry frequency
   - verification results
   - execution duration

9. Preserve benchmark conditions:
   - identical initial Git revisions
   - identical tasks
   - comparable model settings
   - identical verification rules
   - documented execution budgets

10. Separate:
   - deterministic offline benchmarks
   - local token estimates
   - live DeepSeek API measurements

11. Support opt-in live evaluation with explicit token and cost limits.

12. Generate:
   - machine-readable JSON results
   - human-readable Markdown reports
   - stage-level token breakdowns
   - V1 versus V2 comparisons
   - regression findings

13. Implement CLI commands:
   - wl stats
   - wl stats feature <id>
   - wl benchmark run
   - wl benchmark compare
   - wl benchmark report

14. Introduce configurable regression thresholds.

15. Document benchmark assumptions and sources of measurement uncertainty.

Token-efficiency requirements:
- Measure complete successful workflows.
- Include failed attempts and retries.
- Avoid counting local cache hits as automatic API token savings.
- Do not report theoretical improvements as measured results.
- Do not introduce LLM-based evaluation where deterministic checks are sufficient.

Testing:
- Usage aggregation
- Missing provider fields
- Cache token accounting
- Retry accounting
- Interrupted workflow accounting
- Benchmark reproducibility
- Report generation
- V1/V2 comparison

Acceptance criteria:
1. Full feature token usage is measurable.
2. Context selection quality is measurable.
3. V1 and V2 strategies can be compared.
4. Reports distinguish real measurements from estimates.
5. Metrics remain correct across retries and resume.
6. No duplicate telemetry infrastructure is introduced.

Out of scope:
- Hosted observability services
- Mandatory LangSmith or Langfuse
- Cloud dashboards
- LLM-based benchmark grading
- New workflow orchestration frameworks

Save using the exact plan ID:
v2-07-token-telemetry

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: Measures quality and end-to-end efficiency of the delivered V2 context stack; the roadmap item is already checked and this plan does not reopen or edit it.

## Dependencies

- [v1-03-deepseek-gateway](../v1-03-deepseek-gateway.md) — normalized usage and atomic reservations/outcomes.
- [v1-08-e2e-validation](../v1-08-e2e-validation.md) — pinned workflow corpus, oracles and offline/live fixtures.
- [v2-03-context-ranking](../v2-03-context-ranking/index.md) — ranked file/symbol signals and retrieval judgments.
- [v2-04-context-budget](../v2-04-context-budget/index.md) — packed fragments, estimator labels and expansion.
- [v2-06-workflow-integration](../v2-06-workflow-integration/index.md) — opt-in V2 stage adapter, telemetry and matched-workflow requirements.

## Requirements Reconciliation

Authority: the original request controls this feature's scope; existing provider/workflow contracts and `.ai-factory/RULES.md` constrain storage, approvals and deterministic verification. This request extends the earlier developer-only benchmark surface (`docs/benchmarks.md`, v2-06 Phase 4); it neither changes existing historical results nor authorizes a new grading/execution path. Project docs can be updated at implementation, not during planning.

| Supported combination and decision | Sources | Verification evidence |
| --- | --- | --- |
| Provider complete, partial, legacy or absent: use canonical reservations/outcomes, preserve provider fields and coverage; subset tokens never add to totals and the checkpoint is not substitute usage. | Original Request §§2–4; `metrics/models.py:normalize_usage`, `metrics/budget.py:charge_for_usage`, `contracts/metrics_collector.py` | Known zero vs null, inconsistent/derived total, cache/reasoning subsets, conservation across dimensions. |
| Retry, failure or interrupt: separate transport retry ordinal, workflow repair count and unresolved attempt; retain spend and trusted task/stage attribution after resume without replaying work. Optional observations cannot block a checkpoint that previously fit. | Original Request §§2–4, 8; `workflow/orchestrator.py`, `workflow/state.py`, `workflow/budget.py`, `workflow/results.py` | Real `verify_then_repair`, interrupted reservation and safe resume, near-limit checkpoint and archive with real journal. |
| V1, V2, admitted fallback, refusal and no observer: expose numeric context observations when present, retain both fallback preparations, never infer quality/cache metrics without evidence. | Original Request §§1, 5; v2-04 and v2-06; `workflow/context_metrics.py` | V1/V2/fallback fixtures; no-dispatch refusal, missing and legacy state, enabled versus disabled cache. |
| Retrieval vs pack evaluation: ranked file/symbol P/R, actual relevant source-token coverage and required-dependency coverage are different metrics. | Original Request §§5–7; v2-03; `benchmarks/fixtures/retrieval/cases.json` | `multi_file_banner`, holdout and tight pack, overlap and cache-state cases; no oracle data in prompts. |
| Offline scripted, local estimates and explicitly live: isolate data and label provenance; fixed conditions except named selector; live must pass both admission limits and cannot continue after an uncertain settlement. | Original Request §§8–12 and token-efficiency requirements; `benchmarks/run.py`; v2-06 Phase 4 | Pinned `multi_file_feature`, exact positive/negative outcomes, live preflight, next-send and child tripwires. No paid test prerequisite. |
| Comparable vs missing/incompatible results: pair the full schedules first; only then apply metric-specific eligibility. No savings claims from unmatched arms, incomplete counters or local cache hits; configured gates surface incomplete evidence. | Original Request §§12–15; `benchmarks/results.py:compare_workflow_context` | Compare valid and drifted conditions, including incomplete arms; threshold, Markdown and JSON tests; installed CLI smoke. |

## Architecture and Decisions

- Existing attempt journal = provider usage and feature-level charge. Existing checkpoint = workflow state, scope debits and bounded numeric observations. New analytics are read projections only; no service, database or duplicated token recording.
- Reuse and package the canonical V1/E2E and retrieval harness for installed `wl benchmark`; retain developer wrappers. Production V1 remains default and benchmarks run only packaged fixtures. Use deterministic checks/oracles and existing approval/isolation paths.
- Publish usage, estimates, local and provider cache signals, budget charge, and verification separately. In-flight and legacy data can be incomplete. Use two independent notions of time: workflow/arm elapsed and summed gateway-attempt latency.
- Historical `verify_then_repair` offline baseline had no retry; it is not evidence of repair correctness. No live baseline is asserted. The representative strict local reduction gate comes from v2-06, not a forecast of DeepSeek savings.

## Phase Index

1. [Phase 1: Single-source usage accounting](phase-01-usage-accounting.md) — Tasks 1–2
2. [Phase 2: Context and workflow stats](phase-02-workflow-stats.md) — Tasks 3–4
3. [Phase 3: Reproducible retrieval evaluation](phase-03-retrieval-corpus.md) — Tasks 5–6
4. [Phase 4: Matched workflow benchmark runs](phase-04-workflow-benchmark.md) — Tasks 7–8
5. [Phase 5: Benchmark CLI, regression gates and rollout](phase-05-benchmark-cli.md) — Tasks 9–10

## Cross-Phase Dependencies

- Task 3 depends on Task 2 for canonical attempt joins and nullable usage semantics.
- Phase 3 is independent of Phases 1–2; the commit groups below are a suggested landing order, not execution dependencies.
- Task 7 depends on Task 4 for the routed analytics projection and Task 6 for canonical packaged fixtures and evaluation variants.
- Task 9 depends on Task 8 for the complete run schema and admission contract.

## Tasks

### Phase 1: Single-source usage accounting

- [x] Task 1: Attribute journaled provider attempts ([details](phase-01-usage-accounting.md#task-1-attribute-journaled-provider-attempts))
- [x] Task 2: Aggregate attempts without double counting ([details](phase-01-usage-accounting.md#task-2-aggregate-attempts-without-double-counting)) (depends on 1)

### Phase 2: Context and workflow stats

- [x] Task 3: Checkpoint workflow and context observations ([details](phase-02-workflow-stats.md#task-3-checkpoint-workflow-and-context-observations)) (depends on 2)
- [x] Task 4: Expose read-only stats commands ([details](phase-02-workflow-stats.md#task-4-expose-read-only-stats-commands)) (depends on 3)

### Phase 3: Reproducible retrieval evaluation

- [x] Task 5: Package and validate one fixture corpus ([details](phase-03-retrieval-corpus.md#task-5-package-and-validate-one-fixture-corpus))
- [x] Task 6: Score ranked hits and packed source ([details](phase-03-retrieval-corpus.md#task-6-score-ranked-hits-and-packed-source)) (depends on 5)

### Phase 4: Matched workflow benchmark runs

- [x] Task 7: Run matched offline workflow arms ([details](phase-04-workflow-benchmark.md#task-7-run-matched-offline-workflow-arms)) (depends on 4, 6)
- [x] Task 8: Gate live evaluation by explicit limits ([details](phase-04-workflow-benchmark.md#task-8-gate-live-evaluation-by-explicit-limits)) (depends on 7)

### Phase 5: Benchmark CLI, regression gates and rollout

- [x] Task 9: Add benchmark commands and threshold gates ([details](phase-05-benchmark-cli.md#task-9-add-benchmark-commands-and-threshold-gates)) (depends on 8)
- [x] Task 10: Document operation and measurement uncertainty ([details](phase-05-benchmark-cli.md#task-10-document-operation-and-measurement-uncertainty)) (depends on 9)

## Commit Plan

- **Commit 1** (after Tasks 1–4): `feat: report journal-backed workflow and context telemetry`
- **Commit 2** (after Tasks 5–7): `feat: score pinned retrieval and workflow benchmark fixtures`
- **Commit 3** (after Tasks 8–10): `feat: expose benchmark CLI with live limits and regression gates`

## Definition of Done

- Full-feature and task-level spend is attributable without merging checkpoint and gateway totals; retries, interrupted and archived evidence are represented accurately.
- Reproducible, independently verified offline workflows and retrieval coverage support eligible V1/V2 comparisons; provider measurements and local estimates are separately labeled.
- Installed stats and benchmark commands, versioned reports, gates, docs and the full locked offline suite pass. Live evidence remains optional and is not claimed until executed.
