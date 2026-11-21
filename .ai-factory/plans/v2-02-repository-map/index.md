<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Compact Repository Map and Dependency Graph

Plan ID: `v2-02-repository-map`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; explicitly select this bundle for implementation)  
Created: 2026-11-21  
Depends on: [v2-01-code-indexer](../v2-01-code-indexer/index.md) (completed)

## Original Request

Plan ID: v2-02-repository-map
Plan Title: Compact Repository Map and Dependency Graph
Milestone: V2 - Context Intelligence

Dependencies:
- v2-01-code-indexer

Goal:
Build compact, deterministic repository maps using the persistent source index and a lightweight dependency graph.

Architecture requirements:
- Reuse CodeIndex.
- Preserve ContextProvider compatibility.
- Do not modify the workflow orchestrator.
- Do not introduce LangGraph.
- Use deterministic local analysis.

Before planning:
- Inspect the actual indexed data model.
- Review existing source retrieval and ContextPack structures.
- Evaluate relevant ideas from Aider Repo Map.
- Avoid copying Aider internals without evaluating dependencies and licensing.

Functional requirements:

1. Implement RepositoryMap as a reusable component.
2. Build a lightweight module dependency graph.
3. Represent:
   - source files
   - modules
   - public classes
   - functions
   - methods
   - imports
   - module relationships
4. Resolve local imports where statically possible.
5. Support reverse dependency lookup.
6. Mark unresolved or ambiguous relationships.
7. Avoid claiming exact runtime call relationships from incomplete static analysis.
8. Rank important modules and symbols.
9. Evaluate simple deterministic ranking before graph algorithms.
10. Generate compact repository map representations.
11. Support global and module-scoped maps.
12. Apply configurable output budgets.
13. Include precise source references.
14. Avoid duplicate signatures and definitions.
15. Reuse index data instead of reparsing files.
16. Support incremental graph updates.
17. Expose RepositoryMap through existing context interfaces.

CLI:
- wl map show
- wl map module <module>
- wl map dependencies <module>
- wl map references <symbol>

Token efficiency:
- No LLM calls.
- Avoid full repository dumps.
- Prefer useful symbols over boilerplate.
- Keep formatting compact.
- Do not include irrelevant modules by default.

Testing:
- Relative imports
- Cyclic dependencies
- Reverse dependencies
- Missing imports
- Ranking consistency
- Large repositories
- Incremental graph updates
- Token budget enforcement

Acceptance criteria:
- Repository maps are deterministic.
- Source relationships are traceable.
- Compact maps fit configured budgets.
- Existing indexing infrastructure is reused.
- No workflow compatibility regressions occur.

Out of scope:
- General-purpose graph databases
- Complete Python call graphs
- Runtime dependency tracing
- LLM-generated module documentation
- Multi-agent analysis

Save using the exact plan ID:
v2-02-repository-map

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes
- Rollout: additive offline commands and explicit context preview; no migrations or runtime dependencies.

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: The requested milestone matches the V2-01 linkage and is not yet in `.ai-factory/ROADMAP.md`; record the user-selected target here without editing the roadmap or treating V1 Hardening or the LangGraph revisit as its replacement.

## Requirements Reconciliation

Authority: Original Request defines new behavior; `.ai-factory/RULES.md` and `.ai-factory/ARCHITECTURE.md` constrain it; the completed [V2-01 plan](../v2-01-code-indexer/index.md) and existing implementation define compatibility. V2-01's “cache is not prompt authority” still holds: indexed metadata is optional untrusted data in a preview, never a workflow scope or approval source. The architecture's older help-only CLI wording is stale and does not override current wiring or V2-01 acceptance.

| Decision / supported combination | Source and reconciliation | Verification evidence |
|---|---|---|
| **Checked lifecycle:** healthy current index (including empty) permits reads; missing, stale, unhealthy or incomplete produces no data and no maintenance. Same generation can reuse graph; compatible changes refresh affected importers; checkout/policy/extractor changes rebuild only after a healthy current read. Only explicit index commands persist data. | Request 15–16; V2-01 cache lifecycle; `CodeIndex.lookup` checks and `_plan_generation` reuse | Tasks 1, 3, 6–7; freshness failures, parser tripwire, target-only update and clean-rebuild equivalence. |
| **Static edges:** exact absolute/relative targets and uniquely supported named imports give provenance only when indexed parents do not block descent. Missing parents may represent namespaces; ordinary modules and colliding parent names can invalidate unchanged importers. Missing targets or unresolved members are distinct from external or runtime claims; wildcard only supports base-module evidence. | Request 3–7, 13, 16; indexed `DefinitionFact`/`ImportFact` are narrower than Python runtime behavior | Tasks 2–3; package/stub, missing, wildcard, reverse and cycle fixtures, plus parent-only transitions without importer reparse. |
| **Query and budget:** global candidates rank within the admitted corpus; unique module selectors yield scoped views or interleaved directions of direct dependencies; symbol references mean definitions and explicit import sites only. Invalid, nonexistent and ambiguous selectors fail safely. A genuinely empty result is distinct from a budget that drops all rows. | Request 8–14, CLI, token efficiency; `ContextConfig` strict defaults | Tasks 4, 6; goldens, relevance/lexical baseline, 2,000-module fixture, one-byte and multibyte budgets, short definition provenance and reverse visibility. |
| **Default vs explicit context:** existing retrieval/protocol/workflow are unchanged. The indexed preview is unstaged and map-only; it requires the builder's checkout/admission policy and intersects simultaneous path filters, including disjoint filters. Incompatible combinations fail before reading the index. No new persistence, tool permissions, or source retrieval ranking. | Request 17 and architecture requirements; V2-01 Tasks 6–7; `ContextBuilder.build`, `ContextPack.repository_map`, `ContextProvider.build` | Task 5; unchanged default JSON/messages, mismatched checkout/policy refusal, separate map limits and exact prompt estimate, no AST/Git during preview. |
| **Representative offline cases:** `bug_fix` proves current definition ranges before and after explicit update; the existing `context/render.py` import of `SymbolRef` proves incoming and symbol evidence, and `multi_file_feature` proves no invented alias for `src.config`. Existing mock-transport feature acceptance still passes. | Request acceptance; V2-01 representative offline case and existing source layout | Task 7; fixture CLI and context preview, full locked suite. |

## Architecture and Decisions

- `cli` / explicit `ContextBuilder` preview → `repository_map.RepositoryMap` → checked `CodeIndex` facts → in-memory dependency graph → ranked view and budgeted rows. Adapt at the builder; the graph is not a dependency of CodeIndex or the workflow. Reads still hash the live admitted corpus; no duplicate parse or database. The graph memo is per service instance, not a new persistent cache.
- Adopt Aider's high-level ideas of compact definition summaries, relevance before formatting, and a small output budget ([documentation](https://aider.chat/docs/repomap.html), [design article](https://aider.chat/2023/10/22/repomap.html)). Its [current implementation](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py) uses Tree-sitter/grep-ast, Pygments fallback, diskcache, NetworkX PageRank and a model tokenizer hook; its sampled fitting permits approximation/slack. None are necessary for indexed Python facts or a hard local cap. Start with integer degree/direct import evidence and test against lexical ranking; reconsider graph algorithms only if measured relevance warrants it.
- Aider's [upstream license](https://github.com/Aider-AI/aider/blob/main/LICENSE.txt) is Apache 2.0; its design article also identifies separately licensed grammar queries. No implementation, query files, rendered excerpts, or dependencies are copied or vendored. Any future reuse must separately review upstream and component licenses and notice obligations.
- No LangGraph, orchestrator changes, runtime imports/tracing, exact call graph, LLM, or graph database. Dynamic imports, control-flow reachability, attribute use, and re-export traversal are not inferred.

## Phase Index

1. [Phase 1: Checked source generation](phase-01-checked-generation.md) — Task 1
2. [Phase 2: Dependency graph and incremental refresh](phase-02-dependency-graph.md) — Tasks 2–3
3. [Phase 3: Compact views and opt-in context preview](phase-03-views-and-context.md) — Tasks 4–5
4. [Phase 4: Offline CLI and acceptance](phase-04-cli-and-acceptance.md) — Tasks 6–7

## Cross-Phase Dependencies

- Task 2 depends on Task 1 for upstream provenance and complete admission; Task 3 depends on Task 2 for catalogs and adjacency.
- Task 4 depends on Tasks 2–3 for graph contracts and refresh; Task 5 depends on Task 4 for projection and limits.
- Task 6 depends on Tasks 4–5 for query contracts and the lazy factory; Task 7 depends on Tasks 5–6 for end-to-end integration and docs.

## Tasks

### Phase 1: Checked source generation
- [x] Task 1: Export checked indexed facts ([details](phase-01-checked-generation.md#task-1-export-checked-indexed-facts))

### Phase 2: Dependency graph and incremental refresh
- [x] Task 2: Resolve traceable static imports ([details](phase-02-dependency-graph.md#task-2-resolve-traceable-static-imports)) (depends on 1)
- [x] Task 3: Refresh affected graph relations ([details](phase-02-dependency-graph.md#task-3-refresh-affected-graph-relations)) (depends on 2)

### Phase 3: Compact views and opt-in context preview
- [x] Task 4: Rank and render compact map views ([details](phase-03-views-and-context.md#task-4-rank-and-render-compact-map-views)) (depends on 2, 3)
- [x] Task 5: Integrate explicit indexed ContextPack preview ([details](phase-03-views-and-context.md#task-5-integrate-explicit-indexed-contextpack-preview)) (depends on 4)

### Phase 4: Offline CLI and acceptance
- [x] Task 6: Expose offline map commands ([details](phase-04-cli-and-acceptance.md#task-6-expose-offline-map-commands)) (depends on 4, 5)
- [x] Task 7: Verify and document repository maps ([details](phase-04-cli-and-acceptance.md#task-7-verify-and-document-repository-maps)) (depends on 5, 6)

## Commit Plan

- **Commit 1** (after Tasks 1–3): `feat(map): derive incremental dependency graphs from checked index facts`
- **Commit 2** (after Tasks 4–7): `feat(map): add budgeted offline queries and indexed context preview`

## Definition of Done

- Checked maps are deterministic across equivalent full and delta builds and traceable to admitted source ranges. No provider calls, unchanged-source reparse, or automatic maintenance on reads.
- All four `wl map` queries and the explicit context preview enforce configured limits. References are explicitly import evidence and definitions, not runtime uses.
- Existing `ContextProvider` behavior, default packs/messages, workflow files and approvals remain unchanged. Focused and full locked offline tests and the mandatory documentation checkpoint pass.
