# Phase 3: Context reuse and cross-worktree boundary

Plan: [index.md](index.md)  
Tasks: 7–9  
Depends on: Phase 2 / Tasks 4–5 for Task 7, Task 6 for Task 8; Task 9 uses Task 4 and Task 7

## Objective

Reuse deterministic retrieval and packing work without reusing prompt content, stale references, or another checkout's authorization.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/retriever.py` | `_execute`, `rank_checked`, `_rank_local` | Live scan, graph/Git reads, lexical backend reporting, final witness and pack callback. |
| `src/lean_weave/context/retrieval_ranking.py` | `RankCandidate`, `dedupe_and_select`, `sort_key` | Pure scoring and selection, mutable candidate objects are not a storage format. |
| `src/lean_weave/context/packing_source.py` | `PackingCatalog.facts`, `PackingCatalog.spans`, `_python_spans`, `_unit_from_span` | Cache structural components; slice current text. |
| `src/lean_weave/context/packer.py` | `_compose`, `_select`, `_assemble`, `expand`, `prepare_expansion_delivery` | Request budgets, counter and receipts remain live. |
| `src/lean_weave/app.py` | `create_retrieval_provider`, `create_context_packer` | Central lazy injection; only retrieval factory adds Git signal. |
| `src/lean_weave/worktree/{manager,registry}.py` | `WorktreeManager.revalidate`, `DirectoryIdentity`, routing identity checks | Existing trusted evidence for explicitly authorized worktree pair. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context_cache/{retrieval,packing,peer}.py` | create | Candidate/ranking, structural-span codecs, and typed peer reuse. |
| `src/lean_weave/{app.py,context/retriever.py,context/packer.py,context/packing_source.py}` | modify | Inject cache, retain witness and budgets on hit. |
| `tests/context_cache/{test_retrieval,test_packing,test_peer}.py` | create | Parity, incremental counters, and cross-worktree proof tests. |

## Task 7: Reuse retrieval candidates and ranking

### Intent

Avoid repeat derivation and ranking using task-aware keys while preserving all existing output, including failure and truncation information.

### Implementation Steps

1. Capture the batch epoch before the live scan starts (no DB connection held). Admit persistent artifact lookups and staging only after a complete live scan with matching repository policy. Pass the batch to graph loading, ranking and packing via an internal optional callback/context parameter; do not change public request/result contracts. In the no-graph branch of `_rank_local`, use the Task 4 `FactCache` for admitted Python paths; keep qualified projection and checked-graph definitions live. A callback may stage rows but only the scan owner flushes after final `revalidate()`. Incomplete scans use the exact old path, with no persistent artifact lookups or writes.
2. Split `_rank_local` at the existing base-scoring block and before graph propagation. Cache path-local `retrieval_candidates`: initial file/symbol signals, bounded lexical masks/anchors and current live-definition IDs/spans (no ranked text). The logical key uses path, matcher/score/codec versions, policy, and a hash of effective prepared phrase, ordered primary/expanded terms/selectors, capped flags and task kind. Proof covers file digest, current definition projection (including source kind/IDs/spans), and that file's live lexical projection. On hit reconstruct fresh `RankCandidate`s; graph propagation never mutates cached base objects. On miss compute only that file's base signals. Keep the existing live batched lexical search and symbol attribution, preserving `rg`/Python fallback and anchor caps; do not treat local caching as caching the observed lexical backend.
3. Add `ranking_result` lookup after live graph/Git input and exact lexical evidence are known but before candidate scoring, propagation, windows, sorting and selection. Logical key: rank/codec/cost-estimator versions, admission policy, fully prepared request hash, task/result kind, effective weights/K/character cap, and output scope (`None` distinct from empty; canonicalize set membership). Proof: sorted full relevant path/digest vector for ranking, including non-Python sources and negative inputs in effective scope, canonical graph projection/availability with relevant graph identity and input mode, admitted Git path signal and availability (`None` distinct from empty and no configured reader), and current lexical evidence/flags. A stable missing-index reason can be keyed explicitly; bypass transient index errors and unstable scan outcomes. Derive this proof from live inputs, never from a stored result or Git HEAD.
4. Persist ordered `RetrievalHit`s, rank-only truncation, and an ordered selected-candidate projection for packing: kind, path/digest, symbol ID and qualified name, definition and window coordinates, source kind, and optional `_anchor` (`path`, `line`, `primary_mask`, `expanded_mask`, `phrase`). Rehydrate fresh candidates preserving order and `None` anchors; check the anchor against the candidate's current file or symbol lexical evidence and bound masks by the prepared term counts. Do not derive the anchor from the hit window: `PackingCatalog.anchor_unit` prefers it over the window start and it can select a different statement. Preserve the selected tuple separately from the public hits, which may be filtered. Reconstruct wrapper/degradation from current inputs. Validate K/result kind, IDs/source kinds, current allowed snapshots and digest/range bounds for all entries (including relation evidence and importers), metadata limits and enums. Skip oversized payloads rather than truncate. Re-run the callback, guards and source witness on every hit.

### Required Interfaces and Contracts

- Store hashed task inputs only. Changed scope, prepared term order, graph, Git or effective settings never reuse wrong ranks. When a relevant file changes the full result is recomputed; unchanged path-local base candidates remain eligible for reuse. No persistent side effects on input rejected by the current retrieval contract.

### Requirement Evidence

Request 2–9, 16, 19–20; v2-03 ranking and source degradation contract and v2-04 `rank_checked` callback semantics.

### Error Handling and Logging

Catch cache-local failures only and run the existing calculation; never translate source/graph failures into cache success. DEBUG candidate/rank kind and counts, WARN bounded reason once; no task, anchors, paths or query hash in logs.

### Tests

`test_retrieval.py`: cold/warm exact result plus callback projections, including a Python lexical anchor not equal to the window or definition start; spies on base/full rank and no-graph AST work. Vary task, ordered query, kind/K/weights/cap, scope (including explicit empty), non-Python file edits, graph and Git states, lexical fallback, incomplete/mid-query mutation, tampered anchors/spans, and unrelated per-file hits.

### Acceptance Criteria

Exact repeat avoids scoring/selection; a changed relevant input produces the same answer as uncached and only invalidates affected components.

### Verification

`uv run pytest tests/context_cache/test_retrieval.py tests/context/test_retriever.py tests/context/test_retrieval_search.py tests/cli/test_context_retrieval.py` — all pass.

## Task 8: Reuse packing components

### Intent

Avoid repeated source-structure derivation without caching assembled packs or token accounting.

### Implementation Steps

1. Inject `FactCache` and `PackingSpanCache` into `PackingCatalog` at scan-bound construction sites in `packer.py` (initial compose and L2 catalogs). Use checked graph or existing facts when available; use the cache at the current live `facts`/`spans` misses. Key the span list by admitted relative path, policy and span contract/parser versions; proof uses canonical digest and lines. Persist only structural bounded coordinates, kind, symbol and deterministic sequence; never persist `SourceUnit.text` or `fragment_id` as authority.
2. Refactor `_python_spans` to distinguish transient parser failure from a valid empty span result without changing caller results. Cache only deterministic outcomes; validate coordinates and order against the current canonical text, then continue slicing live text via `_unit_from_span`. Keep scope, overlaps, rank-driven selection, omitted units, L0/L1/L2 budgets, actual counter selection/fallback, request framing and guarded gateway validation live. These settings are not span inputs; a budget change may hit on spans, but it must evaluate a fresh pack.
3. Use the Task 6 shared header formatter in indexed `map_rows_from_graph`; keep metadata fallback, row origin/ranking/order and scope unchanged. Flush staged mutations only after `rank_checked`'s witness for compose or the existing scan witness for expansion. Keep `prepare_expansion_delivery` on its existing live, read-only path (no persistent lookups, touches or publication there); no cache operation can create, restore or extend session validity. Hold no DB handles in `PackingSession`, injected provider or prompt objects.

### Required Interfaces and Contracts

- `PackingSpanCache.load(snapshot, policy, *, batch, compute)` returns original immutable span tuples. No final pack, measured token count, messages, tool replay, receipts, reasoning or arbitrary model-generated output persisted.

### Requirement Evidence

Request 2–4, 6–9, 16, 20 and token-efficiency requirements; v2-04 revision-bound delivery and independent budgets.

### Error Handling and Logging

Invalid span entries and unstable scans follow existing live computation/failure paths. DEBUG span hit/miss counts only; WARN corruption once per operation.

### Tests

`test_packing.py`: warm parser and indexed L0 header call counts, cold/warm exact serialized pack including the anchor-derived span, scoped L0 exclusion, changed budgets and counter fallback, one-file edits, Unicode/CRLF, unchanged unrelated span hits, stale L2/failed witness, delivery's no-cache-I/O path, no receipts or bodies in persisted rows.

### Acceptance Criteria

Warm structural hits skip AST work while current guards, token estimates and request bytes remain equivalent to uncached.

### Verification

`uv run pytest tests/context_cache/test_packing.py tests/context/test_packing_source.py tests/context/test_packer.py tests/context/test_packing_session.py tests/context/test_packing_expansion.py tests/cli/test_context_packing.py` — all pass.

## Task 9: Gate explicit content-addressed peer reuse

### Intent

Permit narrowly scoped reuse for explicitly authorized, same-project worktree pairs without enabling automatic namespace discovery.

### Implementation Steps

1. Add `peer.py::AuthorizedFactPeer` and a context-managed trusted Python entry point `app.py::open_peer_fact_reuse(manager, *, local_feature_id, peer_feature_id, local_context, peer_context, allowed_paths)`. Resolve both caller-selected feature IDs using `WorktreeManager.revalidate`, require `Ownership.MANAGED`, and verify distinct checkout keys, same resolved common-directory path/device/inode, effective UID access, private cache location and matching effective admission/analysis configuration. Freeze the explicit bounded Python path scope; empty means no reuse. No model/YAML/CLI-provided route IDs or peer paths, discovery, or default session changes.
2. The context manager performs one caller-authorized strict peer scan and yields a short-lived capability that can be passed explicitly to `create_retrieval_provider(..., authorized_fact_peer=...)`; the local scan comes from Task 7. Close the capability on exit, and reject use with any other checkout or policy. Apply it only after a local `FactCache` miss, when both complete live scans independently admit the same relative Python path inside caller scope (and local retrieval scope). Match canonical digest, lines, extractor/codec and frozen policy fingerprint excluding checkout root. Require successful strict opens and current regular-file ownership/permission evidence on each side; revalidate both scans and managed routing before serving a peer hit, and the local scan again at final batch publication. Do not cache capability objects or require dual scans on the normal stage path.
3. Read only the peer's existing path-local facts row with its namespace and exact proof using a read-only handle; no peer writes, touches, migrations, or peer scan launched by the cache. Validate the decoded facts a second time against the local snapshot. On success stage a copy under the local checkout proof; only local stats reflect a hit. Never share paths through enumeration, graph IDs, map fragments, rankings, pack components, or source bodies. Fallback to local extraction if any condition fails.

### Required Interfaces and Contracts

- The same relative path, content hash and semantic config are required but insufficient without live two-sided admission and caller authority. This explicit retrieval route only helps on the live-facts branch; checked graph definitions continue to take precedence. No global content-address table or shared cache between projects.

### Requirement Evidence

Request 17–18 and stage/prompt boundaries in `.ai-factory/RULES.md`. Two genuine worktrees of the same repository can share one proven row; equal content alone cannot.

### Error Handling and Logging

Reject unsafe peers with a generic DEBUG reason and no roots, filenames or route/feature IDs. Unknown, inaccessible, stale or unvalidated peer takes the local path.

### Tests

`test_peer.py`: real managed worktrees through `open_peer_fact_reuse` and `create_retrieval_provider`, peer hit observed by extraction spy with unchanged peer DB; default isolation at identical HEAD; scope exclusion, expired capability/routing, adopted worktree, changed digest, mode/permission or ignore policy, mismatched config, separate repositories, missing/unsafe DB and source-witness race.

### Acceptance Criteria

Only the authorized exact-content case skips extraction across namespaces; ordinary CLI/workflow behavior remains isolated.

### Verification

`uv run pytest tests/context_cache/test_peer.py tests/worktree tests/cli/test_feature_isolation_e2e.py` — all pass.

## Phase Risks and Mitigations

Git, lexical fallback and source authority stay live; proof mismatch or race never silently degrades to a stale result. Peer reuse is opt-in and deliberately limited to Python facts with two independent admissions.

## Phase Completion Checklist

All three tasks and their commands pass; update only their checkboxes in `index.md`.
