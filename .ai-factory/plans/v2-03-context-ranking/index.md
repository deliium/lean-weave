<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Task-Aware Context Retrieval and Ranking

Plan ID: `v2-03-context-ranking`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; explicitly select this bundle for implementation)  
Created: 2026-11-21  
Depends on: [v2-01-code-indexer](../v2-01-code-indexer/index.md) and [v2-02-repository-map](../v2-02-repository-map/index.md), both completed.

## Original Request

Plan ID: v2-03-context-ranking
Plan Title: Task-Aware Context Retrieval and Ranking
Milestone: V2 - Context Intelligence

Dependencies:
- v2-01-code-indexer
- v2-02-repository-map

Goal:
Implement deterministic retrieval and ranking of source code relevant to a specific development task.

Before planning:
1. Inspect the V1 Context Builder.
2. Inspect CodeIndex and RepositoryMap.
3. Identify current retrieval limitations.
4. Preserve existing ContextProvider contracts.
5. Reuse current search infrastructure.

Requirements:

1. Implement a ContextRetriever.
2. Support hybrid retrieval using:
   - exact symbol matches
   - file and module names
   - lexical search
   - import dependencies
   - reverse dependencies
   - Git diff relevance
   - related test files
   - repository map importance
3. Reuse ripgrep or SQLite FTS5 where appropriate.
4. Normalize ranking signals.
5. Support configurable ranking weights.
6. Rank both files and source symbols.
7. Prefer specific relevant definitions over unrelated full files.
8. Support:
   - bug fixes
   - feature development
   - refactoring
   - test generation
   - verification failure diagnosis
9. Implement bounded deterministic query expansion.
10. Deduplicate overlapping candidates.
11. Handle incomplete or outdated indexes.
12. Provide fallback to basic text search.
13. Return source provenance.
14. Include ranking explanations and estimated source cost.
15. Support configurable top-k selection.
16. Expose retrieval through ContextProvider.

Expected result fields:
- source path
- symbol identifier
- line range
- relevance score
- ranking reasons
- related dependencies
- estimated token cost

CLI:
- wl context search <query>
- wl context rank <query>
- wl context explain <query>

Token efficiency:
- No LLM-based retrieval or reranking.
- No full-file loading by default.
- Avoid repeated candidate selection.
- Optimize relevant information per token.
- Preserve essential dependencies.

Evaluation:
- Create task fixtures with known relevant files.
- Measure Precision@K and Recall@K.
- Compare against V1 retrieval.
- Document ranking failures.
- Avoid tuning exclusively against one repository.

Acceptance criteria:
- Relevant files and symbols can be retrieved.
- Results include explainable ranking.
- Retrieval is deterministic.
- Quality can be measured.
- Existing V1 workflows remain compatible.

Out of scope:
- Embeddings
- Vector databases
- LLM reranking
- Automatic semantic summarization

Save using the exact plan ID:
v2-03-context-ranking

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes
- Rollout: additive offline preview; no schema migration, new runtime dependency, or enablement in stage builds.

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: The roadmap already marks the index/map milestone complete; task-aware ranking is a requested follow-on to the delivered foundations. Leave the roadmap unchanged.

## Requirements Reconciliation

Authority: The Original Request defines the new capability. `.ai-factory/RULES.md` and `.ai-factory/ARCHITECTURE.md` constrain source admission, stage scope and network behavior. The completed dependency plans and current code establish compatibility constraints, not new ranking behavior. No separate research was used.

| Decision / supported combination | Source and reconciliation | Verification evidence |
|---|---|---|
| **Inputs:** explicit local query; supported admitted text for files and Python for definitions; queries, paths named in prose, cached facts, and Git are evidence, not admission or approval. No result/state persistence. Invalid input does not start discovery. | Request 1–3, 7–9, 11–16; RULES and V2-01 source admission | Tasks 1–2 and 5: policy, validation and protected config test. |
| **Text evidence:** one bounded file scan and one batched literal evidence computation; rg on stdin for small input, Python otherwise or if rg fails. File anchor caps do not hide symbol matches. No FTS or new persistent cache. Full files are not emitted or placed in prompts; bounded local reads are needed to screen and search them. | Request 2–3, 10, 12 and token efficiency; V1 `context/search.py` | Tasks 2 and 4: backend parity, deep symbol match, one-scan and one-pass tripwires. |
| **Checked index:** complete/current, active scan with matching frozen policy may use graph facts and exact IDs; only resolved non-self one-hop static relations influence dependency/degree scores. No recency or runtime-use claims. | Request 2, 11, 13; V2-01 cache lifecycle, V2-02 typed graph | Tasks 1, 3 and 4: policy mutation, detached scan and digest checks, reverse and ambiguous imports. |
| **Fallback:** absent/stale/unhealthy index allows local text plus bounded Python facts, no graph authority. A stable partial scan skips index and Git; invalid Python still produces file evidence. Changed witnesses suppress results; unavailable root returns empty with diagnostic. No auto update. | Request 11–13; V2-01 checked facts rules | Tasks 3–4: missing, stale, incomplete, unavailable, and mid-read tests. |
| **Git evidence:** preview-only staged/unstaged path intersection, no patch/history/body input, no spawn against non-direct roots. Clean differs from unavailable; Git and map importance only boost admitted qualified candidates. | Request 2 and 11; RULES and V1 Git preview guard | Tasks 3–4: staged/unstaged, worktree gitfile and unrelated-change tests. |
| **Tasks and selection:** explicit task profile (`bugfix`, `feature`, `refactor`, `test`, `diagnose`), all/file/symbol result modes, configurable integer weights and top-k. Relevance, evidence and estimated range cost are returned; no code is executed or emitted. | Request 4–10, 13–15, expected fields | Task 4: score goldens, deterministic ties, clipped evidence, dependency coverage. |
| **Compatibility:** current `ContextProvider.build` and all stages remain as-is, including metadata-only plan/verify, scoped implement/retry, and separate write approvals. Add a concrete typed preview `retrieve` method, no protocol member or prompt pack change. | Request 16 and acceptance; RULES and ARCHITECTURE workflow boundaries; V2-02 context preview | Task 5: protocol injection, byte-compatible old output and lazy stage tripwires; Task 6: offline workflows. |
| **Command modes:** plain `search QUERY` remains literal/exact as currently documented; `--task-aware` chooses hybrid search, and `rank` / `explain` always use it. Empty or degraded V2 results retain the same JSON envelope. | Request CLI and V1 acceptance; `cli/commands/context.py::search_command` | Task 5: installed aliases, config round-trip, conflict validation and JSON parity. |
| **Evaluation:** paired offline V1 and V2 labeled rankings on pinned fixtures plus distinct development and holdout trees, with Git disabled on both sides. A separate dirty-checkout comparison isolates the Git signal. Actual V1 prompt excerpts, V2 window cost and retrieval metrics are separate observations. | Request evaluation and acceptance; `benchmarks/fixtures.py` and V1-08 fixture contracts | Task 6: `bug_fix` / `multi_file_feature`, fixture runner, isolated Git comparison, full regression. |

## Architecture and Decisions

- **Current retrieval limits:** `ContextBuilder` ranks names and paths, applies scope filtering after default AST discovery, and considers an optional map before excerpts. Literal body search is separate; `relevant_paths` is not a ranked list. V1 dependency coverage depends on which modules were parsed. V2’s checked graph provides typed directions and source coordinates, but rendered map rows are lossy and a generic checked read scans the whole Python corpus. Git recency is not diff relevance. These are reasons for an additive retriever, not changes to V1 stages.
- **New data flow:** `CLI` → explicit provider `retrieve()` → `ContextRetriever` → one strict admitted source scan → in-memory lexical evidence + checked graph from the same scan (or live AST fallback) + optional Git path flags → fixed seed expansion → normalized rank → deduplicated window references and cost. The final source witness must still match. Neither the lexical backend nor the graph confers source or tool authority.
- **Scoring contract:** deterministic integer normalization and task-kind weight multipliers, one hop and no reranking requests. Definitions and lexical windows usually cost less than whole files; graph-related paths remain discoverable. Rank is not a promise of runtime relationships or verified prompt savings.
- **No changes to** orchestrator, state, prompt policy, write approvals, index maintenance, or configured runtime dependencies. No embeddings, vectors, LLM reranking, or generated summaries.

## Phase Index

1. [Phase 1: Retrieval contracts and source evidence](phase-01-retrieval-inputs.md) — Tasks 1–2
2. [Phase 2: Checked graph evidence and hybrid ranking](phase-02-hybrid-ranking.md) — Tasks 3–4
3. [Phase 3: Opt-in provider, CLI, and quality evaluation](phase-03-provider-cli-evaluation.md) — Tasks 5–6

## Cross-Phase Dependencies

- Task 2 depends on Task 1 for canonical source/query contracts.
- Task 3 depends on Task 1 for a shared strict scan; Task 4 depends on Tasks 2–3 for lexical anchors and graph/diff evidence.
- Task 5 depends on Task 4 for the retriever API; Task 6 depends on Tasks 4–5 for stable rankings, CLI diagnostics and offline regression coverage.

## Tasks

### Phase 1: Retrieval contracts and source evidence
- [x] Task 1: Define retrieval and admission contracts ([details](phase-01-retrieval-inputs.md#task-1-define-retrieval-and-admission-contracts))
- [x] Task 2: Prepare one bounded lexical pass ([details](phase-01-retrieval-inputs.md#task-2-prepare-one-bounded-lexical-pass)) (depends on 1)

### Phase 2: Checked graph evidence and hybrid ranking
- [x] Task 3: Assemble checked graph and Git signals ([details](phase-02-hybrid-ranking.md#task-3-assemble-checked-graph-and-git-signals)) (depends on 1)
- [x] Task 4: Rank files and symbols with provenance ([details](phase-02-hybrid-ranking.md#task-4-rank-files-and-symbols-with-provenance)) (depends on 2, 3)

### Phase 3: Opt-in provider, CLI, and quality evaluation
- [x] Task 5: Expose opt-in provider and CLI commands ([details](phase-03-provider-cli-evaluation.md#task-5-expose-opt-in-provider-and-cli-commands)) (depends on 4)
- [x] Task 6: Measure ranking quality across fixture repositories ([details](phase-03-provider-cli-evaluation.md#task-6-measure-ranking-quality-across-fixture-repositories)) (depends on 4, 5)

## Commit Plan

- **Commit 1** (after Tasks 1–3): `feat(context): add bounded query and checked source evidence`
- **Commit 2** (after Tasks 4–6): `feat(context): rank task-relevant source and expose offline queries`

## Definition of Done

- Offline task queries retrieve files and definitions with stable provenance, reasons, dependency evidence and estimated source cost. Missing or unusable index/Git inputs have tested, explainable fallbacks.
- Default literal search, V1 context packs and workflow stages remain compatible. No new retrieval call sends data to an LLM, runs project code, widens approval, or mutates the checkout.
- Paired V1/V2 fixture metrics and ranking failure notes are reproducible; focused tests, the full locked offline suite, and the mandatory docs checkpoint pass.
