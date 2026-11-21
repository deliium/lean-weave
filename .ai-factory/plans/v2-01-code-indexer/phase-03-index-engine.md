# Phase 3: Incremental publication and symbol lookup

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: Tasks 1–4

## Objective

Implement `CodeIndex` over the shared admission, AST and SQLite layers. Publish exactly one complete source generation, and serve deterministic lookups with provenance.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/repository.py` | `FileSnapshot.digest`, `FileSnapshot.lines`, `Repository.refresh` | Canonical per-file revision and line conventions; create a fresh repository per command. |
| `src/lean_weave/context/python_index.py` | `PythonIndex.lookup`, `parse_module` | Preserve current preview API; V2 exact lookup has a separate typed contract. |
| `src/lean_weave/context/models.py` | `normalize_relative_path`, `SymbolRef` | Use the shared path validation, not persisted paths to open source. |
| `src/lean_weave/context/search.py` | `search` | Existing lexical API remains the place for literal discovery. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/code_index/index.py` | Create | Reconcile complete source manifests, inspect freshness, query definitions, clear fixed cache names. |
| `src/lean_weave/code_index/{__init__,models,sqlite_store}.py` | Modify phase-created files | Public `CodeIndex` and result types; ordered statements and verified scoped lookup. |
| `tests/code_index/{test_incremental,test_lookup}.py` | Create | Parse counts, revision behavior, ordered retrieval and provenance. |

## Task 5: Reconcile source generations transactionally

### Intent

Read and hash admitted input each update, but only parse newly added, text-modified or extractor-invalidated files. Publish no changes after an uncertain scan.

### Implementation Steps

1. Add `CodeIndex(root, *, context_config, state_dir=".lean-weave", protected_paths=(), cache_base=None)`. Keep initialization lazy. Give `build()` and `update()` a common reconcile path under one exclusive lease, with a fresh strict scan on each call. Build bootstraps missing storage; update requires prior initialized storage and returns `missing` with a build hint otherwise. Both reuse facts on existing healthy storage; neither is a corruption reset. Require `scan.complete` and call `scan.revalidate()` immediately before the live commit (or first-build publication); abort on failure instead of calling a second body scan. A working tree is not an atomic filesystem snapshot: this is best-effort race detection around an observed generation, not a claim about later edits.
2. Compute `policy_key` from a versioned canonical JSON tuple of the source-policy version, state path, protected paths, ordered exclusions and candidate cap (scan/entry caps are operational completeness limits). Use Task 4’s `source_revision()` for generation computation; track the parser/extractor key beside it, not in place of it. Under identical admission/bytes, a Git-only metadata/HEAD change is deliberately the same source revision; a checkout/switch/reset/staged or unstaged edit that changes admitted bytes or paths alters the generation on next scan. Ignored paths do not affect revision unless admission changes. No Git diff/status subprocess is needed.
3. Compare by path first. Same path and compatible digest/extractor carries forward ID and facts; refresh byte size if only canonicalization differs. Same path with new content keeps the file ID and replaces facts. Match a departed path to a new path as moved only for a digest unique in **both entire manifests**, never ambiguous duplicates or copies. Carry forward the file ID; reuse facts for a compatible extractor, otherwise parse the new source. Do not infer a rename when both path and text change, or when old content remains at its old path; classify as remove/add. For remaining newly observed paths derive file ID from SHA-256 of a format tag, checkout key, generation revision, path and digest. Allocate all inserts/updates in deterministic path order and fail closed on a collision.
4. Cache syntax/parse-limit outcomes keyed just like successes. Validate extracted facts and path-derived metadata before persistence; apply the same credential canaries to names/signatures/decorators/import metadata, independent of the source scan. If this second check detects a value the source scan did not detect (for example after AST normalization), fail the operation with `unsafe_facts` before publication; never write that path, digest or facts. In this exceptional case the operator must remove the sensitive value or exclude the source; do not stamp an unverifiable revision. Cascade removal and insert/replace facts for changed and invalidated files under the same transaction as metadata and checksum; no-op updates leave the generation unchanged. Establish explicit bounds: one DB of at most 64 MiB via SQLite page limits plus bounded row hydration consistent with configured source limits. Exceeding bounds is an incomplete/budget error, not a partial commit.
5. On first build initialize only after a complete scan. `build` may discard screened pending files from a failed first build under the lease, but never open a pending DB as live. Pending-only storage is missing to reads/update. For a new DB, commit and close `index.pending.sqlite3`, require its journal gone, fsync the file, revalidate the scan and atomically rename within the cache directory, then fsync that directory. Before rename, failure leaves no current generation. If directory fsync fails after rename, return `publication_uncertain` and tell the operator to recheck via `status`; do not retry or undo the rename automatically. For an existing DB use the transactional store. Preserve old rows when scan/extraction/storage fails before commit; no stale row may bypass current admission.

### Required Interfaces and Contracts

- `build() -> IndexUpdate`, `update() -> IndexUpdate`. The immutable result carries revision, schema version, and counts `added`, `modified`, `moved`, `removed`, `unchanged`, `invalidated`, `parsed`, `syntax_failed`, and safe omission counts. Change classifications partition paths (one move consumes one old and one new); `invalidated` overlaps classification and `parsed` counts actual AST calls. A failed scan reports `incomplete` with no success result.
- Policy exclusions/quarantine evict formerly indexed rows on the next successful writer. No history retention; deleting rows and `clear` are not forensic erasure guarantees. No content-based inference from ignored or rejected files.

### Requirement Evidence

Request 1–6, 8, 10, 12–15 and acceptance clauses; reconciliation rows **Partial scan**, **Move or duplicate content**, **Fresh and invalid Python**, **Checkout and revision**, **Store lifecycle**.

### Error Handling and Logging

DEBUG deterministic reconcile counts and reason-coded misses; INFO committed counts and revision; WARN `incomplete`, quarantine totals, supported recovery refusal, or `capacity_exceeded`. Never print file paths, text, names, signatures or SQL values.

### Tests

`tests/code_index/test_incremental.py`: full scan and fresh reopen, parse spy for unchanged/update/change plus syntax-error cache; same-size/mtime rewrite detected by digest; CRLF-only change no parse; exact rename zero reparses, rename+edit remove/add, copies/duplicate moves not paired, delete cascades, ignore/secret transition purges prior rows, extractor invalidation reparses unchanged content, partial traversal, witness failure or failed write leaves previous manifest unchanged, and scan order permutations yield equal generation/counts. Inject first-build failure before rename and directory-fsync failure after rename; verify pending-only behavior, safe rebuild or `publication_uncertain` and actual state on reopen. Cover checkout changes, dirty/untracked changes and unchanged admitted corpus across Git-only changes. Use synthetic credential canaries only.

### Acceptance Criteria

Repeated builds/updates have zero AST calls for unchanged matching entries; move/delete behavior is reproducible, and neither an interrupted build nor unsuccessful update publishes partial data.

### Verification

`uv run --locked pytest tests/code_index/test_incremental.py tests/code_index/test_sqlite_store.py`.

## Task 6: Provide checked deterministic reads and clear

### Intent

Expose storage health separately from source freshness. Return a genuine no-match only for a complete matching revision and never return cached source body text.

### Implementation Steps

1. `status()` first validates storage in a read-only transaction. For healthy storage, policy or extractor mismatch means `stale`; otherwise scan with strict admission, without parsing. Incomplete or failed `scan.revalidate()` means `unknown`; a matching revision means `current`; a complete mismatch means `stale`. Missing/incompatible/corrupt/unavailable storage uses `unknown`. No reader repair or cache creation. Lookup retains the scan session through a final revalidation after collecting hits so a detected race is an error even if the result would have been empty.
2. Add `lookup(name, *, path=None, limit=None)` returning a typed `SymbolLookup`. Exact case-sensitive match on simple, local-qualified or module-qualified name; module hits use module name, kind `module`, and nullable range only for empty files. Module symbols for parse-invalid admitted files remain module metadata with `parse_status`; no fabricated definitions/imports. Validate before I/O: name is nonblank strict UTF-8, at most 4096 bytes, and has no ASCII controls or credential canaries (do not trim or case-fold). A supplied path must satisfy Task 1's canonical relative Python path checks, including no absolute/drive prefix or credential canary. `None` limit uses `search_hits_max`; reject bools, non-integers and nonpositive values, and clamp larger integers. A syntactically valid but excluded/absent path yields zero matches only after a healthy/current freshness check; do not expose why it was absent. Use bound binary names/scope; join `files`, project qualified names using the V1 joining rule, and union module hits. Sort by `(path, start_line or 0, end_line or 0, qualified_name, kind, ordinal)`, taking at most limit+1 ordered rows to set `truncated`.
3. Produce `IndexedSymbol` with `file_id`, `symbol_id`, path, canonical digest, source revision, module/name/kind, nullable range, signature/decorators and parse status. Derive definition ID from SHA-256 of `(file_id, local qualified name, kind, per-name occurrence)` and module ID from `(file_id, "module")`. Compute occurrence over the full definition group ordered by `(start_line, end_line, ordinal)` before filtering/truncating; do not derive it by enumerating returned hits. Unrelated line shifts and an unambiguous move do not change IDs; reordering duplicate definitions may change occurrence association. Paths and ranges always refer to the admitted canonical snapshot, not to cached text. Provide no public historical or unverified lookup.
4. `clear()` holds an exclusive lease and validates all affected leaves before any unlink. Using descriptor-relative operations, unlink the live DB first and fsync its directory before removing its rollback journal; then remove the validated journals/pending files and fsync the directory again. Never remove the lock/directory or unsafe objects. On I/O failure return `clear_incomplete`; retrying confirmed clear resumes safely. An orphan live journal after interruption still yields `recovery_required`; do not treat it as a fresh namespace or allow first build to overwrite it. Missing is idempotent; no schema acceptance needed for clearing a corrupt/foreign DB at this explicit disposable location. Operator confirmation is enforced by CLI.

### Required Interfaces and Contracts

- `IndexStatus` contains health, freshness (`current`, `stale`, `unknown`), nullable stored revision and schema version, and nullable counts derived from validated storage. `SymbolLookup` contains checked revision, sorted immutable hits and `truncated`; no results returned for unknown/stale caches. Shared private scoped-read primitive for Phase 4 accepts current `FileSnapshot`, not arbitrary stored paths or a global freshness claim.
- Scoped provider hits require live admission and exact per-file checks even if the generation status is current; a cache miss always leaves the V1 parser available.

### Requirement Evidence

Request 1, 5–7, 9, 11–13 and 15; reconciliation rows **Fresh and invalid Python**, **Store lifecycle**, **Cache reuse**. V1-04 — accurate references and bounded responses.

### Error Handling and Logging

DEBUG health/freshness and hit counts only; WARN stale/unavailable and safe refusal codes; no selector, rejected path, signature, or DB contents in logs. Status returns an inspection result; lookup raises `CodeIndexError` on noncurrent input rather than pretending to find no hits.

### Tests

`tests/code_index/test_lookup.py`: duplicate names with exact case, module and qualified names, nested/async definitions and `.pyi`, limit and deterministic sort, IDs invariant across selector/limit changes, empty module and invalid syntax, Unicode range sliced from canonical source, stable IDs across line shift and exact move, invalid selector rejected before I/O, excluded/absent path yields zero only when current, and stale/incomplete/unavailable results. Inject a witness change before returning status/lookup and corrupt hydrated rows. Create a recoverable orphan journal by interrupting clear after the live unlink: ordinary commands refuse it and repeated confirmed clear removes it without touching neighbors. Spy that status/lookup never parse or call the gateway.

### Acceptance Criteria

Lookup produces ordered, correctly bounded references or a typed refusal; status distinguishes missing, corrupt and stale; confirmed clearing affects one checkout only.

### Verification

`uv run --locked pytest tests/code_index/test_lookup.py tests/code_index/test_incremental.py`.

## Phase Risks and Mitigations

- A digest is a source identity, not a rename oracle: pair only unique whole-manifest matches.
- Hashing is still I/O on each maintenance/read freshness check; ordinary ContextProvider stages use a scoped cache path rather than a whole-project freshness check.

## Phase Completion Checklist

- Tasks 5–6 meet their acceptance criteria and verification passes.
- Update their checkboxes only in `index.md`.
