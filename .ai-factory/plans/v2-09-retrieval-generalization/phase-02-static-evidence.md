# Phase 2: Static evidence and closure

Plan: [index.md](index.md)  
Tasks: 2  
Depends on: Phase 1 / Task 1

## Objective

Produce exact, bounded dependency evidence using the already admitted Python source and map facilities.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/retriever.py` | `ContextRetriever.rank_checked` | Callback owns the checked scan and ranked candidates; do not run another retrieval. |
| `src/lean_weave/repository_map/service.py` | `scoped_graph_for_scan`, `RepositoryMap.graph_for_retrieval_scan` | Scoped graph has import statuses and source spans from live facts; ranking graph is a reduced projection, not complete binding evidence. |
| `src/lean_weave/repository_map/models.py` | `ImportRelation`, `MapSymbol`, `SourceRef` | Per-alias status, ordinal and exact symbol/range when resolved. |
| `src/lean_weave/context/packing_source.py` | `PackingCatalog.symbol_unit`, `SourceUnit`, `slice_lines` | Existing complete source materialization and canonical slices. |
| `src/lean_weave/context/python_index.py` | `extract_python_facts` | Fact status can be truncated; missing rows are not proof of absence or uniqueness. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/dependency_coverage.py` | create | Scan-local binding analysis, exact proof and bounded deterministic traversal. |
| `src/lean_weave/context/packing_source.py` | modify | Add exact import unit materialization, never structural promotion or string synthesis. |
| `tests/context/test_dependency_coverage.py` | create | Focused static analysis and bounded closure tests. |

## Task 2: Resolve referenced definitions from admitted evidence

### Intent
Resolve what a selected fragment actually uses, not whichever definition happens to rank first in an adjacent module.

### Implementation Steps
1. In `dependency_coverage.py` build a lazy scan-local AST projection keyed by admitted `(path, digest)`; use the same normalized text as `PackingCatalog`. Resolve loads within complete emitted Python statements and definitions by original inclusive lines, including definition headers, decorators, base classes, defaults, bodies, and direct AST annotations. Use full-file AST for lexical ownership, **not** as renderable context. For non-Python return `not_applicable`; partial/invalid Python yields `analysis_unavailable`, never a fabricated binding. Ignore attribute names not rooted in an eligible imported module. Do not execute Python, import dependencies, infer runtime object members or evaluate string annotations.
2. Use a conservative lexical binding table. Only resolve unique unconditional top-level imports or top-level `def`, `async def`, `class` bindings for an unshadowed name; handle parameter, assignment, nested definition, comprehension and local-import shadowing explicitly, and conservatively skip scopes with unsupported `global`, `nonlocal`, class-scope/annotation semantics, wildcard or dynamic rebinding where applicable. Resolve outer evaluation scope for decorators/defaults and base expressions separately from the body. Multiple assignments/redefinitions, conditional bindings (including guarded imports), uncertain scope, string forward annotations, and dynamic access produce typed uncertainty rather than guessed edges. Builtins and purely local names require no closure; proven imported external names retain their relevant import statement but get no external/source lookup. If external vs hidden/absent cannot be proved, record `unresolved_import`, no candidate names or paths.
3. For `from x import Y as Z` join the loaded `Z` to a verified `ImportRelation` by importer path/digest, AST statement/alias order and inclusive span; require a unique exact definition in target snapshot. For `import x.y as z`, `import x.y` and a proven qualified attribute chain, match the imported module prefix/binding first, then require a unique top-level symbol in that module. Allow an evidenced package submodule from `from . import module`; independently reject a relative level that climbs above the top package. Do not chase arbitrary re-exports or choose an ambiguous module or signature. Check target and importer revision, scope, occurrence and span against `PackingCatalog`, and verify the target AST has the unique unconditional binding (a parentless indexed fact is not sufficient); ambiguous matches emit no target. Same-file references use unique top-level definitions from admitted facts, excluding local variables and assignments from the initial definition contract.
4. Preserve entire original import statement only if at least one binding is referenced and the declaration is not already covered, keeping comments/multiline text and co-declared aliases intact. An independently proven binding licenses that original declaration even when the referenced target remains unresolved; it never licenses a speculative target. Only accept independently bounded source lines; if semicolon siblings would emit unrelated statements, return uncertainty instead of slicing/reconstructing source. Report `same_file_definition`, `import_declaration`, `imported_definition` or `qualified_module_definition` with load line, source/target refs and evidence. Accept an already covered declaration as added cost zero. Group each proven definition with any required import slots; no partial publish of a mandatory group.
5. Traverse breadth-first from seed units with stable ordering `(depth, seed order, reference line/column, target path, start/end, relation kind)`; root depth 0, crossing a definition edge adds one, retaining its import does not. Deduplicate by `(path, digest, start, end)`, stop cycles without double charge, and emit at most one record per distinct origin/target/kind. Traverse already-covered defs when needed, but charge only additions. Bounding rules: inspect at most 4,096 AST nodes per inspected source block and 256 distinct references total across all trials in a composition/expansion; memoize per scan, stop on fixed work cap and report `dependency_analysis_limit`. At the configured depth boundary evaluate only direct references of the frontier to distinguish already-covered/cyclic targets from blocked new definitions; never materialize beyond the limit.

### Required Interfaces and Contracts
- Internal frozen `DependencyRef` (path, digest, inclusive range), `DependencyLink` (origin, reference line, target, relationship, selection reason, depth), `DependencyIssue` (origin ref, stable reason, optional admitted target ref). `DependencyAnalyzer(catalog, graph).discover(seeds, *, covered, max_depth)` returns breadth-first proof groups, uncertainty and blocked frontier; not prompt data and not a cache entry. No graph query may open additional files.
- Keep lookup and emit scopes distinct. Use the map's scoped factory over **all** snapshots already admitted by the current checked read scan; an output-only preview filter must not narrow that lookup universe and accidentally certify a duplicate module. `PackingCatalog` still restricts every emitted ref to emit scope. Workflow proves only within its existing frozen approved read universe, not that runtime Python resolution is globally unique. If the unique target is not emittable, report `dependency_uncertain:scope_unavailable` without emitting its path. Incomplete scan or truncated importer/target facts cannot prove a resolved import/definition edge; retain independently proven import declarations where possible and report the evidence gap. Do not infer missing targets, nor use the reduced rank graph as proof.

### Requirement Evidence
- Original Request FR 1–4, 6–7; index combinations **Enabled**, **Uncertainty**, **Scope/cache**.

### Error Handling and Logging
- Missing/unsupported proof produces typed issue; actual source/graph drift fails closed. DEBUG counters for scanned references, proof types, ambiguities and cap stops; never log source, unresolved spelling, or disallowed locations.

### Tests
- Original inline trees for alias/relative/module resolution, multiline relevant import only, duplicate modules/definitions (including one outside an output-only scope but inside the checked scan), same-file refs, annotation/class/base/default cases, negative shadowing (locals, comprehension, conditional and wildcard), already-covered cycle, frontier depth, AST work cap, incomplete scan, truncated facts, non-Python and parse failure. Verify every accepted source span against admitted fixture bytes.

### Acceptance Criteria
- Exact deterministic proof/uncertainty with no speculative source, no ranking change and no new I/O surface.

### Verification
- `uv run --locked pytest tests/context/test_dependency_coverage.py tests/repository_map/test_retrieval_graph.py` — proof and existing graph regressions pass.

## Phase Risks and Mitigations
- False static confidence on Python's dynamic semantics: resolve only a deliberately narrow subset and disclose unsupported bindings.

## Phase Completion Checklist
- Verify Task 2 including negative scope/provenance tests; update `index.md`.
