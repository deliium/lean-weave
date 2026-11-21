<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Incremental Code Indexing and Symbol Database

Plan ID: `v2-01-code-indexer`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-21

## Original Request

Plan ID: v2-01-code-indexer
Plan Title: Incremental Code Indexing and Symbol Database
Milestone: V2 - Context Intelligence

Dependencies:
- Completed MVP 1
- v1-04-context-builder
- v1-08-e2e-validation

Goal:
Extend the V1 Context Builder with persistent, incremental source indexing and deterministic symbol lookup.

Project architecture:
- Python package: lean_weave
- Public CLI: wl
- Canonical entrypoint: lean_weave:main
- Minimal Python workflow orchestrator
- LangGraph deferred under ADR-002

Before planning:
1. Inspect the existing ContextProvider.
2. Identify reusable Python AST and ripgrep functionality.
3. Examine existing SQLite and state storage.
4. Review source inclusion/exclusion policies.
5. Reuse existing code instead of implementing parallel indexing components.

Requirements:

1. Introduce a persistent CodeIndex abstraction.
2. Use SQLite for indexed source metadata.
3. Support content-hash-based incremental updates.
4. Detect added, modified, moved, and removed files.
5. Extract Python:
   - modules
   - classes
   - functions
   - methods
   - signatures
   - decorators
   - source ranges
   - imports
6. Preserve stable source identifiers where possible.
7. Support deterministic symbol lookup.
8. Respect .gitignore and configured exclusions.
9. Reject paths outside the authorized repository.
10. Handle invalid syntax and unsupported files gracefully.
11. Support schema versions and safe index migrations.
12. Detect stale or corrupted index entries.
13. Avoid indexing secrets or excluded generated artifacts.
14. Support index updates after Git worktree changes.
15. Keep indexes isolated by project and source revision.
16. Integrate with the existing ContextProvider.

CLI:
- wl index build
- wl index update
- wl index status
- wl index clear

Token efficiency:
- Zero LLM calls during indexing.
- No repeated parsing of unchanged files.
- No LLM-generated descriptions of every file.
- Index metadata must not automatically enter prompts.
- Preserve accurate source references for future retrieval.

Testing:
- Full indexing
- Incremental updates
- File deletion and renaming
- Ignored paths
- Syntax errors
- Unicode source
- Duplicate symbol names
- Corrupted indexes
- Git worktree isolation

Acceptance criteria:
- Only changed or invalidated files require reparsing.
- Indexed symbols resolve to correct source ranges.
- Index updates are deterministic.
- Existing V1 context operations remain functional.
- Indexing requires no LLM calls.

Out of scope:
- Embeddings
- Vector databases
- LLM semantic summaries
- Background indexing services
- Full multi-language support

Tree-sitter may be evaluated but must not become
mandatory without demonstrated benefit.

Save using the exact plan ID:
v2-01-code-indexer

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes
- Runtime dependencies: standard-library `sqlite3` and existing runtime dependencies only.

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: User-selected target for the first V2 context capability; this milestone is not yet recorded in `.ai-factory/ROADMAP.md`, which remains unchanged. Add it separately through the roadmap owner before requiring a strict match. Do not close or conflate it with V1 Hardening or the LangGraph revisit.

## Requirements Reconciliation

Authority: No formal source hierarchy is declared. The Original Request sets additive V2 behavior. `.ai-factory/RULES.md` and `.ai-factory/ARCHITECTURE.md` constrain boundaries; the completed V1 plans and code define compatibility, not the new SQLite contract. V1-04's non-persistent context is extended by an optional disposable cache, not replaced. V1-08 and `docs/mvp1-readiness.md` establish completed **offline** MVP readiness; live DeepSeek capture is not a V2 prerequisite. The architecture’s “help-only feature commands” line is stale and does not override its workflow wiring or released V1 CLI.

| Decision / supported combination | Source and reconciliation | Verification evidence |
|---|---|---|
| **Admitted corpus:** authorized root + nested `.gitignore` + additive `context.exclusions` + hard denies + bounded safe reads. Both Git and plain roots accepted. Only admitted `.py`/`.pyi` become facts; excluded/generated/quarantined/unsupported paths never become rows. | Request 8–10, 13; V1-04 Requirements Reconciliation and `context/repository.py` / `policy.py`. Strict scan refines omissions without silently changing V1. | Task 1 source-policy and symlink tests; Task 5 exclusion eviction. |
| **Partial scan:** indeterminate ignore, traversal/aggregate-byte exhaustion or a changed scan witness → do not publish or infer removals; read freshness `unknown`. A known oversized source is a deliberate omission; an oversized `.gitignore` is indeterminate. Complete empty scan → valid empty revision. | Request 4, 8–9, 12–13; V1-04 gated reads; `repository.py::_walk_dir` currently sorts before applying its cap. | Tasks 1, 5–6 bounded entry enumeration, changed/missing ignore witnesses, and empty-corpus cases. |
| **Fresh and invalid Python:** compatible same digest reuses facts (including syntax diagnostics); changed digest or extractor mismatch reparses; invalid AST has module metadata but no definitions. | Request 3, 5, 7, 10, 12; V1 parser/line conventions. | Tasks 2, 5–6 parse counts, UTF-8 and line verification, duplicate/case tests. |
| **Move or duplicate content:** unique exact move preserves identity; duplicate/copy or rename plus content change is remove/add where applicable; deleted/out-of-policy rows removed only on complete commit. | Request 4, 6 and deterministic updates; content hashes do not prove intent. | Task 5 unique rename, ambiguous copies, rename+edit and deletion. |
| **Checkout and revision:** distinct worktrees at same HEAD have distinct namespaces; edits or checkout replacements are verified against admitted content, not HEAD. Stable checkout plus changed content publishes a new current generation; old generations are not queryable. | Request 14–15; architecture Worktree and state separation; V1-08 isolation. | Tasks 3, 5, 9 linked worktrees, Git-only vs content change, dirty state and independent clear. |
| **Store lifecycle:** missing build creates; missing update refuses; eligible writes validate before opening writable; known upgrade writes transactionally. A live rollback journal prevents automated recovery; confirmed `clear --yes` then `build` recovers the disposable cache. A pending first build is never current. | Request 2, 11–12 and CLI; `workflow/state.py` atomicity/refusal precedent, not shared persistence. | Tasks 4–6, 8 version, rollback, busy, corruption, interruption and recovery coverage. |
| **Cache reuse:** validated full-revision lookup refuses stale/unknown; provider can use verified per-file facts after live admission despite unrelated revision drift, otherwise reads existing parser output. Lookup validates selectors and assigns duplicate occurrence IDs before limiting results. | Request 6–7, 9, 12, 15–16 and token/prompt policy; V1-04 scoped retrieval. | Tasks 6–7 checked lookup, stale/cache fallback, byte-stable prompt. |
| **Provider stages:** plan/verify no scan or DB access; implement keeps one-hop and gated tests; retry emits only admitted affected/blocker paths. | Request 16 and V1 acceptance; V1-04 stage requirements and `context/provider.py`. | Task 7 lazy injection, scope, budgets and exact messages. |
| **Offline representative case:** build/update and range verification on the existing Python `bug_fix` fixture, plus a warm-cache `wl resume --approve` in the actual feature worktree. Index commands make no gateway calls; feature regression uses the existing mock transport. | Request testing/acceptance; V1-08 fixture corpus and `docs/mvp1-readiness.md`. | Task 9 `benchmarks/fixtures/manifests/bug_fix.json`, `tests/cli/test_feature_offline_e2e.py`, installed CLI and locked offline suite. |

## Architecture and Decisions

- Compose the current `Repository` (policy and snapshots) and `context/python_index.py` (AST extraction and V1 projection) with `lean_weave.code_index.CodeIndex`. The persistent writer is an explicit CLI operation; provider reuse is read-only and after live admission.
- SQLite is for one current observed content generation per canonical checkout, outside the worktree under a private cache path. The production workflow keeps `JsonCheckpointStore`; existing `SqliteSaver` is spike-only and not a runtime dependency. No Git objects, routing state, checkpoints, provider journal or Git history enter the cache.
- Reuse V1 ripgrep through `context/search.py` for its existing literal-search use cases. It already reads admitted content over stdin and falls back to Python. It cannot resolve definitions and is not called for full indexing, reconciliation, or exact symbol lookup.
- AST remains sufficient for required Python metadata. No Tree-sitter dependency or separate evaluation task without evidence of an unmet parsing requirement. No embeddings, vectors, semantic summaries, indexing daemon, multi-language parsing, or orchestration redesign. `wl` and `lean_weave:main` stay canonical; minimal Python orchestration and ADR-002 remain in force.
- Index output is local metadata, not authority or a prompt input. No automatic updates: the operator explicitly updates after edits/switches; stale cache entries cause fallback. Status scans and hashes but does not parse. Source ranges correspond to the canonical snapshot and digest; they are not a promise that a live editor cannot change a file a moment later.

## Phase Index

1. [Phase 1: Shared admission and Python facts](phase-01-source-facts.md) — Tasks 1–2
2. [Phase 2: Isolated SQLite storage](phase-02-storage.md) — Tasks 3–4
3. [Phase 3: Incremental publication and symbol lookup](phase-03-index-engine.md) — Tasks 5–6
4. [Phase 4: Context integration, command surface and offline acceptance](phase-04-integration.md) — Tasks 7–9

## Cross-Phase Dependencies

- Tasks 1 and 2 are independent prerequisites for persistent writes.
- Task 3 has no source/AST dependency; Task 4 depends on Task 3 for containment, Task 1 for shared path validation, and Task 2 for typed facts.
- Task 5 depends on Tasks 1–4 for complete scans, facts, identity and transactions. Task 6 depends on Task 5 for revision and recovery semantics.
- Task 7 depends on Task 6 for checked scoped fact reads; Task 8 depends on Task 6 for public maintenance methods and Task 7 for matching app wiring. Task 9 depends on Tasks 7–8.

## Tasks

### Phase 1: Shared admission and Python facts
- [x] Task 1: Add strict source admission for index maintenance ([details](phase-01-source-facts.md#task-1-add-strict-source-admission-for-index-maintenance))
- [x] Task 2: Share path-independent Python extraction ([details](phase-01-source-facts.md#task-2-share-path-independent-python-extraction))

### Phase 2: Isolated SQLite storage
- [x] Task 3: Establish checkout and cache boundaries ([details](phase-02-storage.md#task-3-establish-checkout-and-cache-boundaries))
- [x] Task 4: Define schema validation and safe migrations ([details](phase-02-storage.md#task-4-define-schema-validation-and-safe-migrations)) (depends on 1, 2, 3)

### Phase 3: Incremental publication and symbol lookup
- [x] Task 5: Reconcile source generations transactionally ([details](phase-03-index-engine.md#task-5-reconcile-source-generations-transactionally)) (depends on 1, 2, 3, 4)
- [x] Task 6: Provide checked deterministic reads and clear ([details](phase-03-index-engine.md#task-6-provide-checked-deterministic-reads-and-clear)) (depends on 5)

### Phase 4: Context integration, command surface and offline acceptance
- [x] Task 7: Reuse verified SQLite facts in the ContextProvider ([details](phase-04-integration.md#task-7-reuse-verified-sqlite-facts-in-the-contextprovider)) (depends on 6)
- [x] Task 8: Expose offline index maintenance commands ([details](phase-04-integration.md#task-8-expose-offline-index-maintenance-commands)) (depends on 6, 7)
- [x] Task 9: Validate and document the offline workflow ([details](phase-04-integration.md#task-9-validate-and-document-the-offline-workflow)) (depends on 7, 8)

## Commit Plan

- **Commit 1** (after Tasks 1–4): `feat(index): add shared Python facts and contained SQLite storage`
- **Commit 2** (after Tasks 5–9): `feat(index): add incremental lookup and offline maintenance commands`

## Definition of Done

- Unchanged compatible admitted files require zero reparses across successful updates; complete changes, moves and deletions are deterministic and invalid syntax remains non-fatal.
- Checked lookups return correct decoded ranges and source identifiers; corrupt, stale or foreign rows cannot be presented as current. No unsupported migrations or cross-worktree cache sharing.
- `wl index build/update/status/clear` works offline and cannot invoke the LLM. V1 context packs, tool outputs and offline workflow behavior remain compatible.
- Focused and full locked test suites pass with representative fixture coverage, and the mandatory docs checkpoint is complete.
