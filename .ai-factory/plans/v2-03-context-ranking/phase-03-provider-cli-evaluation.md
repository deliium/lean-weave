# Phase 3: Opt-in provider, CLI, and quality evaluation

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: Phase 2 / Tasks 3–4

## Objective

Expose the same offline retrieval result through the concrete ContextProvider and the CLI, then measure it against V1 retrieval without changing an existing workflow decision.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/contracts/context_provider.py`, `src/lean_weave/context/provider.py` | `ContextProvider.build`, `DeterministicContextProvider` | Ordinary workflow providers and test doubles need only implement `build`. |
| `src/lean_weave/app.py` | `create_context_provider`, `_protected_index_paths`, `create_repository_map` | Construction is lazy; use matching checkout/policy and preserve injected-provider precedence. |
| `src/lean_weave/cli/commands/context.py`, `src/lean_weave/cli/runtime.py` | `search_command`, `_emit`, `command_context` | `search` already has literal and exact symbol modes. Keep their flags, exit codes and payloads intact. Help does not load config. |
| `benchmarks/fixtures.py`, `benchmarks/fixtures/manifests/bug_fix.json` | `materialize`, pinned offline fixture checkout | Reuse a representative real project artifact, not only mock ranking rows. |
| `src/lean_weave/context/ranking.py`, `src/lean_weave/context/builder.py` | `rank`, `Ranked.files`, `ContextBuilder.build` | `relevant_paths` is alphabetized; use the actual V1 rank order for Precision@K and Recall@K. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/provider.py`, `src/lean_weave/app.py` | modify | Add explicit retrieval capability and lazy wiring without touching stage behavior |
| `src/lean_weave/cli/commands/context.py` | modify | Add `rank`, `explain`, and opt-in `search --task-aware` |
| `tests/cli/test_context_retrieval.py`, `tests/context/test_retrieval_provider.py` | create | CLI envelope, installed entry points, config round-trip, lazy wiring, and offline acceptance |
| `benchmarks/retrieval.py`, `benchmarks/fixtures/retrieval/cases.json`, `benchmarks/fixtures/retrieval/trees/` | create | Reproducible labeled corpus and comparison runner |
| `tests/benchmarks/test_retrieval.py` | create | Metrics and fixture regressions |
| `docs/context-retrieval.md` | create | Contract, configuration, examples, failure analysis |
| `docs/cli.md`, `docs/architecture.md`, `docs/benchmarks.md` | modify | Cross-links, V1 boundary and evaluation invocation |

## Task 5: Expose opt-in provider and CLI commands

### Intent

Give local users and embedders a way to retrieve task-relevant source without sending it to a model, changing stages, or breaking existing literal search.

### Implementation Steps

1. Add `DeterministicContextProvider.retrieve(request: RetrievalRequest) -> RetrievalResult` and optional keyword-only `retriever_factory`. Cache the factory-created retriever per provider instance for map memo reuse; never call it from construction or `build`. With no factory, construct a retriever over a fresh strict repository using the provider's resolved root/config/state directory and live fallback (no index). For a typed caller that only needs retrieval, export a separate structural `RetrievalProvider` protocol from `context/retrieval_models.py`; do not add any member to the stage `ContextProvider` protocol.
2. In `app.py`, add `create_retrieval_provider(context, *, workspace_root=None, cache_base=None) -> DeterministicContextProvider`. Factor deterministic provider construction so existing `create_context_provider` still honors both injection mechanisms before choosing the default; default concrete instances expose the same retrieval method. Configure a lazy `ContextRetriever` factory over a strict `Repository` and a `RepositoryMap` bound to the same checkout, exclusions, state-directory and protected config path (reuse `_protected_index_paths` and `create_repository_map`); inject `RepositoryMap.graph_for_retrieval_scan` and bind `read_retrieval_diff` to `verify.process.run_bounded` here. The explicit CLI factory selects the built-in preview adapter, not arbitrary injected stage providers; document that distinction. Use `TYPE_CHECKING` for model-only annotations if required to preserve existing import boundaries; no function-local imports or eager cache I/O.
3. In `cli/commands/context.py`, add `wl context rank QUERY` and `wl context explain QUERY` and route them plus `wl context search QUERY --task-aware` through one helper that validates a `RetrievalRequest` and invokes `retrieve` exactly once. All three accept `--task-kind`, `--kind`, `--top-k`, and `--json`; no caller workspace selectors, Git revisions, regexes or write flags. Existing `search QUERY [--path|--symbol|--limit|--json]` still performs literal/exact lookup; reject mixing the three old selectors with `--task-aware`, or new retrieval flags without it, before opening the repository/cache. Use `None` defaults in Typer for search-only detection, and the model for effective defaults. `rank` and `explain` do not accept V1 selectors.
4. Reuse safe JSON/text emission. All three serialize the same `RetrievalResult` object plus `command` (`search|rank|explain`); the optional search mode adds `mode: "task-aware"`. Empty/degraded results still exit zero and return the same envelope, with `hits: []` when empty and fixed diagnostics when applicable. Text search prints path, range and qualified name; rank adds score and cost; explain also prints weighted reasons, related dependency coordinates and diagnostics, even when there are no hits. Route invalid combinations/invalid values through generic existing CLI usage errors and do not echo raw query. Keep `wl` and `lean-weave` on the existing Typer app. `config.loader.dump_config` already serializes model defaults, so `wl init` adds the new nested settings without a separate template change.

### Required Interfaces and Contracts

- No changes to stages, ContextBundle/messaging, orchestrator, state schema, `context build`, `--indexed-map`, V1 `search` mode output, or approvals. Verification diagnosis is an explicit `--task-kind diagnose` query, not an automatic retry change. Programmatic results are metadata, not a tool or prompt authorization.
- CLI examples: `wl context search "banner"` (literal), `wl context search "fix banner" --task-aware`, `wl context rank "fix banner" --task-kind bugfix --kind symbol --top-k 5 --json`, `wl context explain "why does route fail" --task-kind diagnose`.

### Requirement Evidence

- Original Request 8, 13–16, CLI and acceptance; reconciliation **Compatibility**, **Command modes**, **Tasks and selection**. `.ai-factory/RULES.md` and V1 stage behavior override any suggestion to use generic checkout search for workflow retries.

### Error Handling and Logging

- Invalid flags/typed inputs use safe usage messages and make no source/cache calls; help does not load config. Log DEBUG factory selection and INFO verb/result count; never echo query, source, Git metadata or raw validation errors.

### Tests

- `test_context_retrieval.py`: Typer tests for all three verbs, typed options, rejected mixtures, help with invalid config, cold/warm JSON with expected evidence, unchanged envelope on empty/degraded results, text escaping and no body output. Golden-test V1 literal and symbol JSON against existing assertions; `context build` and indexed-map remain unchanged. In an offline temp checkout with the pinned `bug_fix` fixture, exercise both installed entry points (sibling scripts under the running environment's scripts directory, following `benchmarks/offline.py::run_installed`'s sanitized environment). Run `wl init`, reload the generated YAML, override one nested field, and assert both executable spellings yield the same parsed `rank --json` object with the effective settings and `add` evidence. Isolate `HOME` and `XDG_CACHE_HOME`; no live API key.
- `test_retrieval_provider.py`: fake retriever called only by `retrieve`; constructor and stage builds do not start it; custom legacy provider with only `build` still accepted and chosen. Use checkout-local protected alternate config in a provider query; network/provider, cache mutation and workspace-write tripwires.

### Acceptance Criteria

- Retrieval is available via the concrete provider and all CLI surfaces; the original provider protocol and existing workflow/preview outputs remain compatible.

### Verification

`uv run --locked pytest -q tests/cli/test_context.py tests/cli/test_context_retrieval.py tests/context/test_provider.py tests/context/test_retrieval_provider.py tests/test_app_context.py tests/contracts tests/repository_map/test_context_preview.py` — all pass.

## Task 6: Measure ranking quality across fixture repositories

### Intent

Make quality, regressions and limitations measurable offline instead of declaring improvement from a single happy path or from token estimates alone.

### Implementation Steps

1. Add `cases.json` with stable case ID, repository selector, split (`development|holdout`), task kind, query, nonempty `relevant_paths`, optional gold symbol keys (`path`, qualified name, start line), and optional expected source-line intervals. Freeze labels before looking at rank results. Include existing pinned `bug_fix` (`src/calc.py::add`) and `multi_file_feature` (`src/config.py`, `src/app.py::banner`) using `benchmarks.fixtures.materialize`; add at least two distinct labeled synthetic trees, one development and one held out, with unequal directory depth and different identifiers. Cover all five task kinds plus exact, lexical-only, duplicate definitions, reverse import and related tests, a non-Python or syntax-error cue, and at least one plausible unrelated decoy per new tree. A refactor case should need an incoming importer; a diagnosis case should need a concrete line window and not raw check logs. Use stock weights on held-out trees. Do not reuse the `insufficient_context` case as a recall target; it remains a closed-scope workflow control.
2. Add `uv run --locked python -m benchmarks.retrieval --json` command (developer tooling, not a `wl` command). Materialize once per repository into scratch, use the same clean bytes and declared fixture policy inputs for both engines, and isolate cache in scratch outside source. Disable Git for this comparison: V1 uses `build_python_index` → `context.ranking.rank` with `ContextQuery(task=query, include_tests=True, allow_git=False)` (no hints); V2 passes `read_diff=None` in both cold and warm modes, with `kind=file` and `kind=symbol` at `top_k=10` before and after explicit `CodeIndex.build()`. Report V1 `Ranked.files` and `Ranked.symbols` in ranking order, not alphabetical `ContextPack.relevant_paths`. Separately record V1 admitted excerpts from `ContextBuilder.build` at `ExpansionLevel.RANGES` with a fixed explicit prompt budget, labeled as actual V1 prompt evidence. Compare the same tasks, with no V1-only expected paths, equal `K`, and no LLM/provider or executing fixture code. V2 references are not prompt excerpts.
3. Report per-case and macro averages split by engine/cache state and by split at K=1, 3, 5, 10. Precision = unique correct entries within first K divided by K; Recall = unique correct entries within first K divided by labeled relevant count. File ranking is by distinct path; symbol labels are counted only for cases with explicit symbol truth and match path + qualified name + original definition start (`start_line` for V1, `definition_start_line` for V2). Compute misses and first wrong paths, reason codes for V2, and whether tagged evidence has a reported source range that covers the gold interval. Report cost separately: cumulative local estimates at K, and labeled interval hits using the longest prefix of ranked V2 ranges fitting 256 and 512 estimated source tokens; do not treat these as actual prompt or live token savings. Stable JSON key and case order; no wall clock or scratch paths in comparison output.
4. Separately measure the Git signal on a development-only synthetic fixture. In a fresh scratch Git repo, commit fixture sources, then make a tracked comment-only edit to a labeled directly matching file and to an unrelated decoy; use comment text that does not match the query or alter the intended labels. Build the index once after the edits and reuse that same dirty checkout/cache for both V2 queries. Compare `read_diff=None` to the production diff adapter, keeping the same weights and inputs. Verify the labeled result receives `git_diff`, the unrelated changed path does not qualify, and report any rank/metric change separately from the matched V1 baseline.
5. Document configuration, supported modes, all reason/score semantics, source bounds (including the single-line cost exception) and no-source-output behavior, degradation, limitations (Python-only definitions, incomplete static resolution, name heuristic, case-sensitive lexical matching, linked-worktree Git unavailable), and a compact cold/warm versus V1 results table plus the Git-only comparison. Include at least two observed misses/regressions with evidence/mitigation, or explicitly say when a designed failure probe did not reproduce; do not tune against the holdout. Record reproducible commands and where to add cases. Complete the mandatory `/aif-docs` checkpoint and link the page from CLI, architecture, and benchmarks docs. Do not edit milestone state in this task.

### Required Interfaces and Contracts

- Unit goldens check designed dominant behavior, not a hand-tuned macro threshold: the existing `add` fixture is first in file and symbol modes cold/warm, each signal-targeted synthetic case should locate its labeled essential evidence in top five in its supported mode. Report aggregate changes versus V1 honestly even when negative. Benchmark is offline, deterministic and documented independently of the existing mock workflow benchmark.

### Requirement Evidence

- Original Request evaluation, token efficiency and acceptance; reconciliation **Evaluation**, **Fallback**, **Compatibility**. V1-08 fixture guarantees and completed V2-01/V2-02 tests remain in scope for regression coverage.

### Error Handling and Logging

- Reject malformed labels, unknown paths or ambiguous symbol targets before scoring. DEBUG safe case ID, engine/cache state and counts, never source or all query strings. Use safe deterministic output; missing Git is an explicit environment limitation, not a network fallback.

### Tests

- `test_retrieval.py`: empty result, short list and duplicate treatment; macro grouping and no-symbol skip; pinned `bug_fix` representative CLI/runner results; designed signals produce their essential results under expected conditions; assert no Git diff reads during matched comparison and isolate the Git boost from changes to the index or checkout bytes. Repeated fixture reports match, including warm on equivalent materialized checkouts (compare stable fixture keys, not checkout-scoped IDs).
- Run existing workflow acceptance (in particular `tests/cli/test_feature_offline_e2e.py`) and locked full suite. No external services or credentials.

### Acceptance Criteria

- Repeatable comparison reports file and symbol Precision@K and Recall@K plus source cost, identifies failures across repositories and confirms V1 workflow compatibility.

### Verification

`uv run --locked python -m benchmarks.retrieval --json` and `uv run --locked pytest -q` — offline metrics are produced and the full suite passes.

## Phase Risks and Mitigations

- Existing `search` semantics are public. Make hybrid search opt-in; document legacy literal behavior prominently.
- Ranking metrics are not prompt quality or live token cost. Report the two separately and use the holdout to detect overfitting.

## Phase Completion Checklist

- Both tasks satisfy acceptance, full locked tests and mandatory docs checkpoint.
- Update only the task checkboxes in `index.md`.
