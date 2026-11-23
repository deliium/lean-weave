# Phase 5: Development-only quality gates

Plan: [index.md](index.md)  
Tasks: 7  
Depends on: Phase 4 / Tasks 5–6

## Objective

Compare the real candidate with V1 and pre-change V2 on only the frozen independent development set and enforce compatibility and token ceilings.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/benchmark/runner.py`, `src/lean_weave/benchmark/retrieval.py` | `run_retrieval_matrix`, `evaluate_retrieval` | Existing entry points load shared corpus data; do not use either for this gate. |
| `src/lean_weave/context/packer.py` | `open_task`, `expand`, `prepare_expansion_delivery` | Compare actual initial and delivered outputs, not previews or observers. |
| `pyproject.toml` | `[project.scripts]`, pytest config | Use actual installed wiring and explicit safe regression paths. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `tests/retrieval_dependencies/dev_eval.py`, `tests/retrieval_dependencies/test_dev_eval.py` | extend | Wire candidate arm, paired comparisons and machine-readable fail gates. |
| `docs/context-packing.md` | extend via `/aif-docs` if recording measured claims | Document verified development-only results and limits, not holdout improvement or release certification. |

## Task 7: Gate recall and token cost

### Intent
Demonstrate development-set improvement or preservation without treating any historical evaluation example as a target.

### Implementation Steps
1. Add candidate arm to Task 1's isolated runner. Use the same installed V2 packer, request (including the ordinary required path), checked index outside checkout, scope and settings as the V2 control, except `dependency_coverage.enabled`. Benchmark cold/warm ContextCache separately. Normalize and compare rerun disabled results with the frozen pre-change V2 baseline and rerun V1 with its frozen implement baseline; mismatched semantic outputs are regressions. Use Task 1's exact registered-symbol expansion rule for both V2 arms and count only `prepare_expansion_delivery`, not `expand` previews. Label V1 final coverage as unchanged initial coverage with L2 not applicable. Report these as packer-versus-reference results; rely on Task 6 separately for sealed workflow and make no workflow-wide token or latency claim.
2. Apply predeclared gates to the production profiles on frozen coverage/control cases: per-case initial and final required-interval coverage may not decrease against either baseline; require at least one strict gain against current V2 and overall missing interval count no worse than either arm. Required anchors and negative controls must hold; intentional budget/uncertainty cases must match their separate expected statuses. Refusal is not token savings. No cap can be exceeded at any send. Where candidate delivers no newly required intervals versus V2, allow at most `max(64, ceil(0.05 * V2 input_tokens))` extra input units at each compared stage; where it improves coverage, allow at most `ceil(0.25 * V2 input_tokens) + 128 * newly_covered_required_intervals` extra input units, still under the hard limits. Apply a suite guard of at most 20% more summed initial input units on ready coverage/control cases than current V2; compare total delivered input (including L2) and report added dependency units and output reserve separately. Apply the same per-case overhead formula to summed input at final delivery. If a baseline refused, disclose it and apply the hard caps/coverage without treating zero input as the comparator. Never alter independent labels, caps or queries to make the gate pass.
3. Assert normalized deterministic results across the 21 measured reps and cold/warm parity excluding runtime counters/timings/receipt IDs. Record paired latency median and p95 with interpreter, config, revision and environment. Flag a repeatable warm p95 regression over both 1.5× V2 and +10ms as a review blocker; corroborate with a second development-only run before blocking for timing. Report expansion frequency and reasons by arm, cap outcomes and complete/missing IDs. The scorecard exits nonzero on deterministic contract/quality failure; don't promote policy or tune to a named example if it fails.
4. Run explicit regression files after confirming collection doesn't read/materialize known holdouts (test imports and fixture loaders included). Never run the shared benchmark or broad `pytest` whose collection could touch the corpus. Preserve the existing behavior/tests without rewriting them. Use original independent examples for any previously benchmark-backed workflow behavior not exercised by those explicit safe suites. Keep results in a new path outside the repo unless writing the frozen independent baseline. Cite manifest hash, revisions and actual command for every development claim. A separate plan must acquire and evaluate an unseen holdout before any generalization or release claim.

### Required Interfaces and Contracts
- Same Task 1 command/output schema plus `candidate`, paired development rows and stable gate outcomes. No live provider, no benchmark registry, no historical artifact reads/writes and no green result from skipped candidate rows.

### Requirement Evidence
- Original Request evaluation discipline, FR 10–12, token efficiency and all acceptance criteria; index **Evaluation** and **Default**.

### Error Handling and Logging
- Nonzero gate exit for incomplete comparable evidence, unmatched baseline hash, budget/coverage regressions or non-determinism; DEBUG aggregate numeric metrics and safe fixture IDs, never task or input text.

### Tests
- Gate tests for refused or missing rows, unchanged-vs-improved token bands, expansion preview vs receipt delivery, disjoint manifests, normalized determinism and timing-warning predicate. Compatibility set exercises trivial existing code paths as well as independent scored cases.

### Acceptance Criteria
- Independent evidence shows no loss against V1/current V2, at least one improvement over current V2, bounded cost and scope, and no generalization claim until blind evaluation.

### Verification
- `uv run --locked pytest tests/retrieval_dependencies tests/context/test_dependency_coverage.py tests/context/test_retrieval_provider.py tests/context/test_retrieval_ranking.py tests/context/test_packer.py tests/context/test_packing_expansion.py tests/context/test_packing_session.py tests/context/test_packing_source.py tests/context/test_packing_tokens.py tests/context_cache/test_dependency_coverage.py tests/context_cache/test_packing.py tests/repository_map/test_retrieval_graph.py tests/workflow/test_context_lifecycle.py tests/workflow/test_context_expansion.py tests/workflow/test_orchestrator.py tests/config/test_context_config.py tests/cli/test_context.py tests/cli/test_context_packing.py tests/cli/test_dependency_config.py tests/providers/test_deepseek_packed_context.py tests/contracts` — audit imports/fixtures before execution; do not add benchmark-backed CLI feature, retrieval or index test files to this run.
- `uv run --locked python -m tests.retrieval_dependencies.dev_eval --arms v1,v2,candidate --output /tmp/opencode/v2-09-dev-comparison.json` — new artifact destination, exit 0 only after all deterministic gates pass.

## Phase Risks and Mitigations
- Shared loader accesses historical cases even when filtered: separate runner, no production benchmark imports and an explicit regression allowlist.
- Development evidence is not blind validation: preserve the historical evaluation unchanged and require a separate unseen holdout.

## Phase Completion Checklist
- Verify gates and ordinary regressions; update Task 7 in `index.md`. Leave release certification and blind validation open for separate work.
