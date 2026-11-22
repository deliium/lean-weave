# Phase 3: Reproducible retrieval evaluation

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: none (independent of stats and workflow accounting)

## Objective

Measure ranked relevance and actual source-pack coverage separately, using the existing corpus as the starting point.

## Current-Code Evidence

| Path | Symbols | Why it matters |
| --- | --- | --- |
| `benchmarks/retrieval.py`, `benchmarks/fixtures/retrieval/cases.json` | `load_cases`, `evaluate_case`, `precision_at_k`, `recall_at_k`, `cost_metrics` | Existing development/holdout judgments, rank metrics and prefix-cost proxy. |
| `benchmarks/fixtures.py` | `load_manifest`, `materialize`, `run_all_oracles` | Pinned V1 workflows and controlled Git origins; keep fixture labels out of candidate context. |
| `src/lean_weave/context/{builder,retriever,packer}.py`, `src/lean_weave/repository_map/service.py` | `ContextBuilder`, `ContextRetriever`, `ContextPacker`, `scoped_graph_for_scan` | Compare production selectors; admit graph and source using existing policy. |
| `pyproject.toml` | `uv_build`, `[project.scripts]` | Checkout-level `benchmarks` is not an installed import dependency; package the single runtime-owned benchmark core and fixture resources. |

## Files to Change

| Path | Action | Required change |
| --- | --- | --- |
| `src/lean_weave/benchmark/{__init__,fixtures,retrieval}.py`, `src/lean_weave/benchmark/fixtures/` | Create/move | Single installable implementation and corpus; keep source, expected edits, labels and oracles distinct. |
| `benchmarks/{fixtures,retrieval}.py` | Modify | Backward-compatible imports and standalone retrieval entrypoint, without duplicate core or data. |
| `src/lean_weave/context/{builder,packer}.py`, `tests/benchmarks/test_fixtures.py` | Modify | Observe existing rank/pack evidence on one admitted snapshot; switch to canonical monkeypatch targets and check package resources. |
| `tests/benchmarks/test_retrieval.py`, `tests/benchmarks/test_retrieval_pack.py` | Modify/create | Golden labels, fixtures, matching and reproducibility tests. |

## Task 5: Package and validate one fixture corpus

### Intent

Make the actual existing corpus available to an installed `wl` and add complete ground truth rather than maintaining a second corpus.

### Implementation Steps

1. Move `benchmarks/fixtures.py`, `benchmarks/retrieval.py` and the curated resources under `src/lean_weave/benchmark/`. Keep compatibility wrappers for existing imports and `python -m benchmarks.retrieval --json`; move internal callers and monkeypatch targets to the canonical modules. In particular, move the `load_manifest`, `read_retrieval_diff` and `evaluate_case` patches in existing fixture/retrieval tests so wrappers do not disconnect the test from the real module globals. Keep historical baselines in place. Load trusted resources relative to the installed package; never import fixture source as application code, discover it from cwd or include bytecode/caches/baselines in the wheel.
2. Extend the existing retrieval case model with inclusive 1-based relevant ranges and distinct required dependency ranges or whole-file IDs. Validate each label against materialized sources/AST once before evaluation; reject duplicate IDs, noncanonical/escaping paths, stale line numbers, manifest/tree mismatch and empty file judgments. A case may intentionally have no symbol labels; that metric is not applicable. Record split, task category, trusted fixture content fingerprint and judge schema version.
3. Retain the development/holdout separation, existing bugfix/feature/refactor/test/diagnosis and flat `src`, package-with-tests and multi-file structures. Add complete labels to existing applicable cases: e.g. `multi_file_banner` labels `src/app.py:4–5` and dependency `src/config.py:1–1`; cover the nested `holdout_billing` relative imports with dependency labels. Include one fixture with a missing required dependency under tight packing and a repeated/overlapping span. Reuse existing repositories before adding fixture trees.

### Required Interfaces and Contracts

- Only packaged, checked-in fixture IDs; no arbitrary repository or manifest input. Materializer leaves labels, edits and oracle programs outside the model candidate tree. Reuse deterministic Git materialization for workflow data and canonical tree hash for retrieval-only data; do not change V1 pinned HEAD values.

### Requirement Evidence

Original Request §§6, 9–10; v2-03 predecessor and existing retrieval cases are the fixture contract. Exercise a real `multi_file_feature` manifest and a held-out repository.

### Error Handling and Logging

Reject invalid corpus before work; DEBUG case/split/counts, WARN stable validation code, never source or task text.

### Tests

Extend `tests/benchmarks/test_retrieval.py` for stale labels and valid AST/ranges. Assert independent fixture copies have equal fingerprints, pinned HEADs remain valid, and labels/edit recipes are outside candidate root. At this task, inspect the built wheel for the full canonical corpus (manifests, trees, edits, oracles and retrieval labels) and absence of bytecode/baselines. In a disposable installation outside the checkout, resolve one pinned manifest and one labeled retrieval case, then materialize the pinned HEAD; Task 9 separately tests installed CLI behavior.

### Acceptance Criteria

Canonical fixtures are readable from one packaged location and old developer imports still work.

### Verification

`uv run pytest tests/benchmarks/test_fixtures.py tests/benchmarks/test_retrieval.py` and `uv build --wheel` followed by the installed-resource check above — existing and new cases pass.

## Task 6: Score ranked hits and packed source

### Intent

Compare selection quality without an LLM judge or substituting candidate cost for selected evidence.

### Implementation Steps

1. Extend installed `retrieval.py` with `evaluate_retrieval(case, variant, scratch)` returning strict, versioned metrics. For V2, project ranked identities from the `rank_checked(..., on_ranked=...)` callback used inside `ContextPacker._compose`, with a local evaluation seam carrying only the evidence the scorer needs; measure pack coverage against the same admitted scan/graph as that rank, not a second `_v2_rank` run. For V1 use the builder's existing ordered rank and source snapshot; where necessary expose its internal rank result as read-only evidence rather than reranking with different access. Score file and symbol lists separately (unique first-seen identity; qualified symbol + path + start line). Precision@K = relevant among top K / K; recall = relevant among top K / all judged relevant; K = 1, 3, 5, 10. No symbol judgments => null, not zero. Report per-case scores and macro average split by development/holdout with denominators.
2. Separately score actual V1 source excerpts or V2 admitted fragments. Match paths and inclusive line intervals, union overlaps per path, then intersect relevant spans. Compute local relevant-source tokens once per merged span with the same pinned source estimator in both arms (record estimator ID; no per-span wire-envelope charge); report numerator plus total selected-source tokens and nullable ratio. Mark a required dependency present only if its whole labeled range/file is covered. Rows, digests, guessed prefix, and omitted text do not satisfy source coverage. Keep old `cost_metrics` prefix proxy separate and do not export `_v1_prompt_excerpts` raw text in the new results.
3. Define fixed retrieval variants: V1 default; V2 default graph/map and pack; V2 no map (`map_tokens=0`, same rank graph); V2 without graph evidence (construct retriever with `load_graph=None` and supply no graph, with map cap zero; passing `graph=None` alone can still use the configured graph loader); V2 tight source (`source_tokens=512` versus default 4096); V2 cache enabled cold and warm versus disabled. For cold/warm use a private cache namespace on the same unchanged checkout: one unmeasured identical prime before warm, assert matching selection/quality and report available real hit/miss counts. Use fresh roots for unrelated arms. Keep disabled cache distinct from enabled-with-zero-hits.

### Required Interfaces and Contracts

- Use existing builder, retriever and packer APIs, with matching fixture query/scope. Record both graph and map-output switches; do not imply the V1 default and V2 budgets are equivalent. Record refusals with reason and no source coverage; no fabricated ranking scores when a selector cannot rank. Git-signal experiments remain a separate developer command.

### Requirement Evidence

Original Request §§1, 5–7, 10 and acceptance 2–4; see ranked-versus-packed and cache rows in the index.

### Error Handling and Logging

Report unsupported combinations or pack refusals explicitly; DEBUG variant, counts and time; WARN missing measurement coverage. No raw source, location payloads, or cache identifier in shareable output.

### Tests

`tests/benchmarks/test_retrieval_pack.py`: exact P/R including short top-K and missing labels; repeated ordering under cold/warm; span union, overlapping judgments, map-only dependency miss, tight-budget missing dependency, deterministic metrics across independent materializations ignoring timings/cache observations. Run the real `multi_file_banner` and `holdout_invoice` labels, not just synthetic intervals.

### Acceptance Criteria

Reports distinguish ranking relevance from actual packed source usefulness for every supported variant.

### Verification

`uv run pytest tests/benchmarks/test_retrieval.py tests/benchmarks/test_retrieval_pack.py` — deterministic quality checks pass offline.

## Phase Risks and Mitigations

- Relevance judgments are incomplete; label holdout scores descriptive and disclose denominators.
- Do not let the packaged oracle, intended edits, or relevance labels enter a benchmark workspace or its review/context allowlist.

## Phase Completion Checklist

- Both tasks meet acceptance and legacy retrieval CLI still runs.
- Mark Tasks 5–6 in `index.md` after verification.
