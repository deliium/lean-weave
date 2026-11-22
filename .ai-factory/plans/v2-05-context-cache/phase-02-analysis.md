# Phase 2: Incremental source analysis

Plan: [index.md](index.md)  
Tasks: 4–6  
Depends on: Phase 1 / Tasks 1–3

## Objective

Reuse per-file facts, affected import resolutions and map fragments while preserving the current admission and map behavior.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/provider.py` | `_facts_session`, `_safe_loader`, `_targeted_index`, `_implement`, `_retry` | Stage code scopes reads before projection; CodeIndex already supplies optional checked facts. |
| `src/lean_weave/context/python_index.py` | `build_python_index`, `extract_python_facts`, `extractor_key` | Digest-checked cache belongs at the raw facts seam, not resolved projections. |
| `src/lean_weave/code_index/index.py` | `fact_loader`, `read_generation`, `read_generation_for_retrieval_scan` | Retain versioned index reads and avoid a second walk for retrieval. |
| `src/lean_weave/repository_map/graph.py` | `_build_catalogs`, `_consulted_keys_for_import`, `_resolve_imports_for`, `refresh_graph` | Existing watch-set strategy and package conflict semantics. |
| `src/lean_weave/repository_map/service.py` | `_load`, `graph_for_retrieval_scan`, `for_context`, `_render_module_symbol_view` | All map paths require checked input; render limits and order remain live. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context_cache/{facts,imports,map_rows}.py` | create | Domain codecs and typed adapters. |
| `src/lean_weave/{app.py,context/provider.py,context/repository.py,repository_map/graph.py,repository_map/service.py}` | modify | Lazy injection and witness-bound lookup/publication. |
| `src/lean_weave/code_index/index.py` | modify | Add internal checked-generation context seam for map consumers needing a final source witness; retain existing public API. |
| `tests/context_cache/{test_facts,test_imports,test_map_rows}.py` | create | Reuse and invalidation with real repositories. |

## Task 4: Reuse admitted Python facts

### Intent

Remove repeat AST extraction across feature executions in the same checkout without widening stage scope or changing CodeIndex semantics.

### Implementation Steps

1. Add `facts.py::FactCache` over the shared cache with `facts_payload` / `facts_from_payload` and current CodeIndex facts safety validation; extract common validation rather than allowing raw payload access. Key uses relative path, shared facts-policy digest (state/protected paths, exclusions, all scan caps, policy version), and `extractor_key()`; proof uses screened canonical digest and line count. Keep the source of admission (`strict scan` versus `strict selected-path proof`) in the live validator, not the raw facts key: both adapters may use one row only after independently proving the same path and policy. Other artifact kinds still key their mode where it can affect output. Validate every definition/import span and text metadata. Cache deterministic `ok`, `ok_truncated` and syntax results; bypass parser resource/limit failures that may be transient.
2. Add lazy `app.py::create_context_cache` and inject into the built-in provider only when enabled; preserve both injected provider override points. Add a small scoped batch abstraction to `FactCache` for lookup/memo/staging. In `_facts_session`/`_safe_loader` with the new feature enabled, load in this order: stage-local memo → checked `CodeIndex.scoped_facts(snapshot)` → `FactCache` → current extractor. Use the single-snapshot index seam in this branch so the index read lease closes before ContextCache I/O; leave the old `fact_loader` session in disabled mode. An individual index miss must not disable new cache access or suppress checks on subsequent files. Stage cache publication follows successful source operations, not constructor/session provisioning.
3. Take keys only from already-read stage snapshots. For new-cache lookup, add an internal `Repository` selected-path check using live descriptor-relative no-follow reads and ancestor/ignore witnesses, including absent ignore files and active config protection. Require the strictly admitted snapshot's digest and line count to match the stage snapshot; use the same facts-policy input builder as rebuild. Revalidate selected witnesses immediately before flushing the stage batch. Do not walk the tree or add source to stage scope; if strict equivalence cannot be demonstrated within the existing configured caps, bypass the new cache for that path and use the existing stage result. Never introduce cache access in plan or verify. Keep disabled stage wiring identical, including existing CodeIndex reads.
4. Use the existing explicit workspace root passed by `workflow/session.py` into `app.py::create_orchestrator`, not controller cwd. A new feature worktree gets a separate namespace; repeated builds/resumes in that checkout reuse entries. Never turn task requests, state, approvals, renderer output or model output into persisted values.

### Required Interfaces and Contracts

- `FactCache.load(snapshot, policy, *, batch, load_index=None) -> PythonFacts` returns the original domain type. Only admit path-local facts, not module projections or resolved import edges. Never load symbols for a path excluded by stage policy to improve cache hit rate.

### Requirement Evidence

Request 2–4, 6–9, 16–20 and `.ai-factory/RULES.md` stage isolation. Checked index hits, index misses, disabled storage, excluded files and source edits are distinct cases.

### Error Handling and Logging

Fallback to extraction on row/store failure, at DEBUG for a normal miss and WARN once per build for corrupt storage. Preserve source diagnostics and never log a task or source payload.

### Tests

`test_facts.py`: two new provider instances, absent/stale index, loader call counts, byte-for-byte stage output, source change in one path, protected/ignored now-hidden path, raced selected-path witness, seeded facts hit through the stage adapter only with an equivalent selected-path proof, no I/O for plan/verify or disabled cache, injected provider and correct workspace root.

### Acceptance Criteria

Unchanged inputs skip new AST work after warmup without changing implement/retry prompts or touching other worktree storage.

### Verification

`uv run pytest tests/context_cache/test_facts.py tests/context/test_provider.py tests/code_index/test_offline_e2e.py` — all pass.

## Task 5: Reuse dependency resolutions

### Intent

Allow a new map service instance to reuse unaffected importer resolutions without freezing graph topology.

### Implementation Steps

1. Add an optional resolver callback to `build_graph`/`refresh_graph`; retain their in-memory fast path and route only currently unresolved/affected importers through the callback. Build a pre-resolution watcher set using `_consulted_keys_for_import` for each import fact. Audit all resolver branches for additional catalog dependencies and use the union with any additional consulted bucket before lookup. Include empty buckets, prefixes, namespace descendants when consulted, package-vs-module classification and exported symbol shape when read. If a branch cannot produce a complete witness, bypass it rather than guess. Use a shared helper for watcher calculation and assert it covers the resolver's observed bucket reads in tests.
2. Key `import_relations` by importer relative path, graph and resolution codec versions, header policy/extractor/schema versions. Proof hashes importer ID/path/digest/facts digest and sorted consulted bucket identities, including each candidate's ID, relative path, digest/facts digest and classification; sort only unordered witnesses. Store checked relation tuples and watched keys; decode against live module/symbol catalogs and verify importer identity, import ordinals, source bounds, membership of candidate/target IDs, relation enums and watched keys. Rebuild graph nodes, adjacency and revision from the current checked generation; do not restore a serialized graph.
3. Inject cache via `create_repository_map`, and stage writes until after the generation/scan's last source revalidation. Keep existing public `read_generation` and `read_generation_for_retrieval_scan` APIs intact. Adapt the internal `CodeIndex._checked_generation` flow for cache-enabled standalone maps: materialize the detached generation under the index read lease, release the index connection **and namespace lease**, then do cache I/O while keeping the matching Python scan open for the final witness. The default index-only call path retains its current behavior. Retrieval uses its existing open scan and outer batch owner. If a witness fails, return the existing fail-closed stale/unavailable result, not cached graph data.

### Required Interfaces and Contracts

- Cache disabled or no checked generation → existing behavior, no auto index build or repair. The global Python revision labels the output; it is not the entire importer key. No cached relation may mint a new source ID or make a path visible.

### Requirement Evidence

Request 2–4, 6–9, 16–17; v2-01 checked reads and v2-02 graph behavior. Target-only add/remove/ambiguity invalidates consumers of that bucket, not unrelated importers.

### Error Handling and Logging

On invalid row resolve that importer live; DEBUG aggregate reused/recomputed counts, WARN bounded corruption/degradation reason only.

### Tests

`test_imports.py`: compare cold and warm to `build_graph`; spy on `_resolve_imports_for` across new instances; compare after target edit, addition/removal, namespace/package shadowing and exported-definition changes; test non-Python edit, stale/corrupt index, tampered IDs and witness loss.

### Acceptance Criteria

Warm checked maps avoid importer resolution; unrelated importers continue to hit when a dependency changes.

### Verification

`uv run pytest tests/context_cache/test_imports.py tests/repository_map tests/code_index/test_retrieval_scan_generation.py` — all pass.

## Task 6: Reuse repository map fragments

### Intent

Cache reusable map rendering work independently of selected scope and token budgets.

### Implementation Steps

1. Extract module header and definition long/short rendering from `_render_module_symbol_view`. In `map_rows.py` expose independent cacheable header and definition variants: the header proof covers live module path/name/digest; the definition proof additionally covers definition identities, spans, kind and normalized signature digests. Store escaped, bounded alternatives and the existing signature-too-long flag; no bodies, ordinary excerpts, budgeted maps, or user material. Key by relative module path, variant and render contract. Reuse the same header producer in `context/packing_source.py::map_rows_from_graph` after its existing scan/digest/scope intersection, via the outer ranking batch (Task 8); do not compute or expose definition variants in the L0 path. Never use row strings as keys or authorization.
2. Validate row shape, known symbol/owner paths, live bounds, per-row size and credential policy. Key and proof omit budgets, selector and importer ranking because only pre-order row formatting is reused. Keep visibility, symbol deduplication, import popularity/order, selected module, scope and `apply_budget` live; preserve existing truncation semantics. For cache-enabled `show`, `module` and `for_context`, render inside the Task 5 checked-generation context and flush the combined graph/rows batch only when that context's final witness passes; obtain rows only for modules selected from the current view graph. The other views revalidate before returning their graph-derived output. The disabled paths keep their current witness/error behavior.

### Required Interfaces and Contracts

- Preserve `MapResult` revision and ordering; different row/token limits may reuse unchanged fragments but never bypass the live budget. Dependency/reference commands use the cached graph relations; their final render remains live.

### Requirement Evidence

Request 2–4, 6–9, 16, 20 and v2-02 checked map views; compare normal, scoped and low-budget paths.

### Error Handling and Logging

Malformed rows trigger local re-render, not a partially applied map. DEBUG kind and count only; do not log rendered rows.

### Tests

`test_map_rows.py`: count render calls; compare cold/warm `show`, `module` and scoped `for_context`, vary row/token budgets and include-tests; change one module, exclude another, and assert unchanged fragments still hit; invalid or credential-bearing row falls back. Task 8 verifies the same header hits in indexed L0 packing without exposing definition variants.

### Acceptance Criteria

Warm results are identical to cold maps and fragments remain reusable across safe budget changes.

### Verification

`uv run pytest tests/context_cache/test_map_rows.py tests/repository_map tests/cli/test_map.py` — all pass.

## Phase Risks and Mitigations

Keep transient source witnesses alive through publication, but release DB transactions before cache writes. If resolver witness completeness cannot be demonstrated for a branch, do not use persisted relations for that branch.

## Phase Completion Checklist

All three tasks and their commands pass; update only their checkboxes in `index.md`.
