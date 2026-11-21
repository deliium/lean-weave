# Phase 2: Dependency graph and incremental refresh

Plan: [index.md](index.md)  
Tasks: 2–3  
Depends on: Phase 1 / Task 1

## Objective

Derive a lightweight static graph with traceable import relationships and refresh only affected importers within a reusable map instance.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/python_index.py` | `DefinitionFact`, `ImportFact`, `_import_facts`, `module_name_for_path` | Names, aliases, relative levels, ordered import spans and definition kinds are already indexed. The older path-only resolver is not lossless and stays unchanged. |
| `src/lean_weave/code_index/models.py` | `FileRecord`, `IndexHeader`, `derive_symbol_id` | Use stored module names and existing identity rules rather than infer a second AST model. |
| `src/lean_weave/code_index/index.py` | `_plan_generation` | Index update reuses compatible unchanged/moved facts; graph must still reconsider importers when target membership changes. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/repository_map/{__init__,models,graph}.py` | create | Typed graph, catalog, import resolver, reverse links and incremental delta application. |
| `tests/repository_map/{test_graph,test_incremental}.py` | create | Static semantics, provenance and delta/full equivalence. |

## Task 2: Resolve traceable static imports

### Intent

Represent source/module/symbol facts and explicit module relationships without pretending to know runtime binding or calls.

### Implementation Steps

1. In `models.py` add frozen `SourceRef(path, digest, start_line, end_line)` with inclusive 1-based bounds and nullable bounds only for file/module references; `ModuleNode` keyed by `file_id` with `FileRecord` metadata; `MapSymbol` with index symbol ID, qualified name, indexed kind, signature and `SourceRef`; and `ImportRelation` with importer ID, import-span ref, statement/name ordinal, normalized spelling, alias, status, reason, and optional resolved target/module symbol IDs. Preserve typed ordered candidates for ambiguity; never pick one by sort order.
2. In `graph.py` build separate catalogs from admitted records: file ID → node, indexed module name → sorted candidate IDs, and `(file_id, simple_name)` → top-level definition IDs (only `parent is None`). Empty/non-identifier module names remain representable as file-only nodes, not resolution candidates; `src/` and `__init__` naming comes from indexed names. `.py` and `.pyi` with the same module, or flat/`src` collisions, are ambiguous, not merged. Rootless/namespace paths may resolve by exact indexed name; do not synthesize namespace nodes or infer parent edges.
3. Produce one relation per import alias including wildcard, keyed by importer ID and statement/name ordinal. For each exact module candidate, check its proper dotted prefixes from shortest to longest. A prefix with no indexed record may be a namespace (do not synthesize a node); a unique `.py` or `.pyi` package initializer permits descent; a unique ordinary module blocks descent (`unresolved/non_package_parent`); a colliding prefix blocks descent (`ambiguous/ambiguous_parent`). `import a.b` resolves only exact `a.b`; no prefix fallback. For `from`, use its absolute base or derive relative base from the importer's indexed module (initializer `.py` or `.pyi` uses its own name, others drop the final component; strip `level-1` package components then append explicit module). No known package or ascent through package root is `unresolved/invalid_relative`.
4. Resolve `from base import item` against the valid exact `base.item` candidate and top-level `item` facts of a valid unique `base`. If `base` is blocked by a proper parent, report that blocker. If `base` itself is a unique ordinary module, do not consider a child module underneath it: a unique indexed definition still gives a `base` edge plus symbol reference; an absent definition gives only a `base` edge with `unresolved_member`. Otherwise duplicated base, duplicated eligible child, multiple definitions, or simultaneous eligible child and definition are `ambiguous`; unique child without competing definition gives a module edge; unique definition without eligible child gives a `base` edge plus symbol reference. When only the base is unique, retain the `base` edge with `unresolved_member` and no symbol reference; wildcard never probes a child and records `wildcard_module_only` only on a valid unique base. If no base and no child, mark `unresolved/not_indexed`; distinguish absence from proven external dependencies. Do not follow re-exports, aliases at use sites, `__all__`, dynamic imports or attribute references. Resolve only existing indexed module names; never synthesize `src.` aliases for the index's source-root convention.
5. Index forward and reverse by resolved target ID per import occurrence, including module-only evidence; unresolved/ambiguous items never produce guessed reverse edges. Build unique neighbor sets for ranking. Ignore self links in rank/neighborhood expansion but preserve their provenance in explicit dependency queries. Cycles use adjacency, never recursion.

### Required Interfaces and Contracts

- `build_graph(generation: IndexGeneration) -> DependencyGraph`; graph exposes read-only ordered nodes, definitions, forward/reverse relations, and explicit import references by resolved symbol ID. Ordering: path, line, statement/name ordinal, target path where applicable. Derive IDs using CodeIndex helpers, including occurrence/ordinal identity rather than line-text heuristics. `syntax`/`parse_limit` nodes can be local module targets but have no imported statements or inferred symbols.
- Graph retains indexed definitions without interpreting visibility; visibility and rendered deduplication are Task 4 concerns. Imports within branches/functions are syntactic evidence only; no execution scope or call claims.

### Requirement Evidence

- Original Request 1–8, 13, 15; reconciliation import-resolution rules. Verify relative, missing, ambiguous, cycle and reverse query cases.

### Error Handling and Logging

- Expected incomplete static resolution is data, not an exception. `DEBUG [RepositoryMap.graph]` aggregate node and status counts only; do not log names, signatures, candidates, or source paths.

### Tests

- `tests/repository_map/test_graph.py`: build checked temporary fixtures with absolute/dotted/alias/multi-name imports, `__init__.pyi`, nested and root-escaping relatives, namespace child, missing/deferred/wildcard members, `.py`/`.pyi` and `src` collisions, child-vs-definition ambiguity, repeated definitions, cycles, self imports and reverse results. Verify `pkg.py` blocks `import pkg.child` but still supplies its own named definition to `from pkg import name`; a colliding `pkg` prefix makes `import pkg.child` ambiguous. Neither blocked edge may appear as a reverse edge. Assert exact ranges for import and definition and no edges from dynamic calls.

### Acceptance Criteria

- Both directions agree on provenance; unknown or ambiguous relations remain distinguishable, and exact references are only those justified by indexed import facts.

### Verification

- `uv run --locked pytest tests/repository_map/test_graph.py` — graph cases pass independently of renderer or CLI.

## Task 3: Refresh affected graph relations

### Intent

Reuse in-memory projections and retain correct edges for unchanged importers after target-only changes.

### Implementation Steps

1. Add `refresh_graph(previous: DependencyGraph | None, generation: IndexGeneration) -> DependencyGraph` to `graph.py`. Compare `(checkout_key, policy_key, extractor_key, schema_version, graph contract version)` for compatibility; rebuild on mismatch. Reuse a same-manifest graph only after a successful checked read; compare manifest as well as revision so identity changes do not leave old IDs. Diff old/new records by file ID and record equality; path-changing moves count as changed projections.
2. Build the next catalogs and derive changed module keys by comparing ordered bucket records for old/new names. For each import, register the absolute base/target and every proper prefix; for named `from` imports register the child key too. Register all keys before branching, even when the current result is blocked, absent, or ambiguous. Relative base changes on a move cause reevaluation of the moved importer itself.
3. Affected sources are added/changed IDs plus previous watchers of changed keys, minus removed IDs. Remove old relation contributions and watchers of affected/removed IDs, then resolve affected sources against the new catalogs and update both adjacency directions and symbol reference indexes. Reuse unaffected relation tuples. Stage the next state, then publish it as a whole; old published graphs cannot mutate. Keep a single in-memory graph per `RepositoryMap` instance and no persistent graph cache. Fresh processes derive from the current index once.

### Required Interfaces and Contracts

- Reject failed generation reads before changing graph state; retained prior graph is never served as current after refusal. Sorted publication gives identical views to full rebuild on the same generation, including target-only imports that become missing, ambiguous, or newly resolvable.
- Linear catalog/diff pass plus work for affected importers, sorting for deterministic publication; no all-pairs scans and no AST access. Existing CodeIndex reads still perform full admission/hash checks. No hidden update operation.

### Requirement Evidence

- Original Request 4–6, 15–16 and incremental/large tests; reconcile target-only edits with V2-01's explicit update lifecycle.

### Error Handling and Logging

- `DEBUG [RepositoryMap.refresh]` cold/hit/delta and affected/removed counts only; a read refusal propagates the original safe code. Do not log source or use prior results as a fallback.

### Tests

- `tests/repository_map/test_incremental.py`: use real `CodeIndex` updates and graph resolver/AST tripwires after index updates. Cover target add/delete/rename/exclude, package member change, alias and range-only changes, collision appearing/disappearing, unchanged refresh, incompatible keys, and snapshot immutability. In one transition series leave `pkg/child.py` and its importer untouched while adding `pkg.py`, adding `pkg/__init__.py`, then removing `pkg.py` to check namespace → blocked → ambiguous → package resolution. Assert graph/signature equality to fresh rebuild, reverse-edge removal/restoration, and that a disjoint importer is not re-resolved. Retain existing index parser-count assertions; add target-only update proof that the unchanged importer isn't parsed.

### Acceptance Criteria

- Changes in target admission/identity invalidate dependent edges without re-parsing unchanged importers; unchanged generation does no graph resolution; full and delta graphs agree.

### Verification

- `uv run --locked pytest tests/repository_map/test_incremental.py tests/code_index/test_incremental.py` — delta correctness and parse counts pass.

## Phase Risks and Mitigations

- Risk: stale incoming edges from a missing/ambiguous target becoming valid. Mitigation: log all consulted names as watchers, not just resolved targets, and compare with a clean rebuild after each test transition.

## Phase Completion Checklist

- Both tasks meet acceptance criteria and verification passes.
- Update only Tasks 2–3 in `index.md`.
