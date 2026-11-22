# Phase 1: Cache contracts and storage

Plan: [index.md](index.md)  
Tasks: 1–3  
Depends on: none

## Objective

Ship a versioned disposable cache that is bounded, isolated, and safe to bypass before any context consumer is connected.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/code_index/identity.py` | `resolve_checkout`, `index_location`, `ensure_location`, `open_lock`, `describe_leaf` | Existing checkout identity, external private path and lease primitives; distinct namespaces are required. |
| `src/lean_weave/code_index/sqlite_store.py` | `_connect`, `_check_schema`, `SqliteIndexStore.migrate` | Existing defensive SQLite and versioning patterns; index table contracts must not change. |
| `src/lean_weave/code_index/models.py` | `source_revision`, `facts_payload`, `facts_from_payload` | Canonical revision and validated facts patterns. |
| `src/lean_weave/config/models.py` | `ConfigModel`, `ContextConfig` | Strict product config with nested optional blocks. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/config/models.py` | modify | Add opt-in `context.cache` configuration. |
| `src/lean_weave/context_cache/{__init__,models,identity,store,service}.py` | create | Public contract, namespace validation, storage and maintenance. |
| `src/lean_weave/code_index/identity.py` | modify | Extract/reuse parameterized private cache-location helpers without changing index paths. |
| `tests/config/test_context_cache_config.py`, `tests/context_cache/{test_models,test_store,test_service}.py` | create | Config, identity, concurrency, migration and recovery tests. |

## Task 1: Define cache contracts and namespace

### Intent

Separate storage identity, logical cache key, and live proof so no persisted entry becomes source or prompt authority.

### Implementation Steps

1. Add strict `ContextCacheConfig` under `ContextConfig.cache`: `enabled=False`, `max_bytes=16_777_216` (1–64 MiB inclusive), `max_entries=4096` (1–16384). No YAML path, cross-worktree, or automatic-rebuild setting. Existing config loads unchanged.
2. Export a versioned `ContextCache` protocol and immutable `CacheKey`, `CacheProof`, `CacheStatus`, `CacheStats`, `CacheOutcome` in `context_cache/models.py`. Public surface: `lookup(key, proof, *, codec) -> CacheOutcome[T]`, `publish(key, proof, value, *, codec) -> bool`, `status() -> CacheStatus`, `stats() -> CacheStats`, `clear() -> bool`, and `rebuild(seed, *, revalidate) -> CacheStatus` (seed receives a writer against a private pending store, not path access). Codec is registered per artifact kind and provides canonical primitive serialization, shape validation and live-domain validation; no pickle or arbitrary Python objects. Return `hit`, `miss`, `invalidated`, or `bypass` with bounded reason enums and no user inputs in messages. Add a `DisabledContextCache` that never resolves roots or performs I/O.
3. Derive `key` from kind, per-kind algorithm and payload versions, canonical effective computation config, and bounded logical input identities. Use domain-separated SHA-256 over compact sorted-key UTF-8 JSON with sorted vectors only for unordered inputs; preserve input order where observable. Proof supplies a kind-specific source/dependency revision plus canonical digests of changing inputs; store the revision with the row. Never persist raw task, hints, request payloads, prompt/tool material, source bodies, or absolute source paths. Reject suspicious metadata with the existing credential filter; no serialization of a whole `FeatureState`, retrieval request, or pack.
4. Generalize the existing private location helper, retaining its fixed index namespace and index behavior. New location: `${XDG_CACHE_HOME:-~/.cache}/lean-weave/context-cache/<checkout-key>/`. Reject locations inside the active checkout or source checkout when distinct; check owner-only paths, leaf type, owner, permissions, single link and no symlinks; validate root identity at use, not at construction. Only fixed database, lock, journal, and pending names are permitted.

### Required Interfaces and Contracts

- `CacheKey` and `CacheProof` are made by typed adapters from freshly admitted inputs, never read from config or selected by the cache. Equality uses both key and proof. Identity includes resolved root/device/inode (`resolve_checkout`), independent of branch or Git common directory.
- Readers supply a kind codec and current proof even on an exact match; getters cannot enumerate arbitrary stored data. `clear` and `rebuild` affect only this checkout's new namespace.

### Requirement Evidence

Original Request 1, 3–5, 16–20; `.ai-factory/ARCHITECTURE.md` CodeIndex and context boundaries. Disabled mode adds no cache I/O; identical hashes alone do not grant cross-worktree access.

### Error Handling and Logging

Invalid internal inputs fail closed as a bypass at the adapter boundary. DEBUG logs carry only kind, event and bounded reason; no digests of task text, filenames, raw exception text, payloads or paths.

### Tests

`test_context_cache_config.py`: defaults, strict ranges and rejected unknown authorization fields. `test_models.py`: canonical order and framing, versions/weights/task changes, wrong kinds, no-op filesystem sentinel; two independently created roots produce separate namespaces even with identical contents.

### Acceptance Criteria

Existing configs are compatible and constructing either implementation never opens or creates storage.

### Verification

`uv run pytest tests/config/test_context_cache_config.py tests/context_cache/test_models.py tests/code_index` — all pass.

## Task 2: Implement bounded transactional storage

### Intent

Allow simultaneous readers while making partial writes, obsolete revisions and corrupt rows unusable.

### Implementation Steps

1. In `store.py` implement SQLite with fixed `application_id`, `user_version=2`, exact schema validation and three tables: namespace metadata (singleton checkout key and sequence), entries (kind, key digest, proof digest, source revision, payload BLOB, envelope checksum, byte length, access sequence; primary key `(kind,key,proof)` and index `(access_sequence,kind,key,proof)`), and bounded counters by fixed kind/event. No dynamic SQL from user inputs. Reuse vetted defensive connection settings or extract a shared helper; SQLite extensions off, trusted schema off, no WAL; use bounded timeout and rollback journal. Open URI `mode=ro` for inspection/reads; an absent cache is a normal miss.
2. Hold a nonblocking/bounded shared namespace file lease around normal DB opens and an exclusive lease for migration, clear and publication by replacement; SQLite serializes short write transactions with `BEGIN IMMEDIATE`. Never hold a read transaction while trying to publish or touch counters. Keep the fixed lock inode across clear/rebuild; perform no DB work if the lease or any fixed leaf fails validation.
3. Exact lookup checks stored length (cap payload at 256 KiB before reading or decoding), canonical envelope SHA-256 over namespace/kind/key/proof/revision/payload, codec and current domain constraints. A proof mismatch for an existing logical key is a logical invalidation and miss, not a deletion of other proofs; unrelated entries and older valid variants can remain until bounded eviction. Use conditional removal of corrupt rows by observed row/version/checksum, never remove a newer replacement. On hit update access order best-effort in a separate short write, batching events/touches for a top-level operation where possible.
4. Charge conservative logical bytes (payload plus fixed overhead) and enforce both configured row cap and byte target (evict least recently used, then stable key order). Set SQLite page size 4096 on creation and `max_page_count` on writers to configured page budget, enforce read-side file-size sanity and reject oversize payloads before insertion. Reuse free pages; no per-request vacuum. On `SQLITE_FULL` roll back, evict cold rows in a separate bounded transaction and retry once, then bypass. If a smaller configured budget falls below existing file size, stop writes and report `capacity` until explicit rebuild under the new cap. Bound rollback journals and at most one pending generation by the page budget; document transient overhead and that the budget is per checkout, not global.
5. Define saturating aggregate counters for hit, miss (including invalidated), invalidation, rebuild, corrupt entry, eviction and bypass. Persist only enum counts, best-effort on healthy storage; a missing read-only status/stats must not create storage. Cache time lives in the offline benchmark, not the LLM ledger.
6. Give each operation-local batch a maintenance epoch captured before its first reuse/computation. Use a fixed-length epoch record in the retained private lock inode, read under the namespace lease. A missing lock is the virgin epoch; the first normal writer initializes that same epoch before use. Under exclusive lease, confirmed clear and rebuild write/fsync a new random epoch **before** deleting or replacing live data. Every delayed flush (including touches and counters) compares its captured epoch under the shared lease and discards the whole batch if it changed. Treat a partial/malformed record as unavailable until confirmed maintenance creates a fresh epoch under exclusive lease. Cap staged rows/bytes by configured limits and the per-row serialization cap; skip excess rather than enlarging the batch.

### Required Interfaces and Contracts

- Use checksum for accidental corruption, not hostile same-UID authenticity; protected directory and live validation are separate boundaries. On busy/corrupt/unavailable state return a typed bypass, never parse an unbounded row.
- Same logical key may have multiple proof variants; partial/stale inputs never become current just because a writer finishes last. Cross-kind and unrelated key eviction happens only under capacity pressure.

### Requirement Evidence

Original Request 6–12, 14, 16, 19. Test two readers, old/new revision writers, proof mismatch, limits and invalid payloads independently.

### Error Handling and Logging

DEBUG: hit/miss/invalidation/eviction and kind; WARN once per operation for unusable store or malformed row; no SQLite paths, SQL text, exception text, or payload. Failure to record counters cannot change the result.

### Tests

`test_store.py`: multiple connections and processes with barriers, no partial visibility or lost successful writes; delayed flush after clear/rebuild (including a batch begun in a virgin namespace); altered checksum, length, payload shape and schema; deterministic eviction, physical page cap and staged-memory caps, oversize bypass, reduced-cap behavior, lock contention and stale variants.

### Acceptance Criteria

Valid entries round-trip concurrently; invalid entries never reach a consumer; row and page caps hold and no request blocks indefinitely.

### Verification

`uv run pytest tests/context_cache/test_store.py` — all pass, including subprocess cases.

## Task 3: Implement maintenance and recovery

### Intent

Make an invalid database replaceable without risking another checkout, the source index, or a last good generation.

### Implementation Steps

1. In `service.py`, implement lazy namespace binding and stable status states `disabled`, `missing`, `ready`, `busy`, `corrupt`, `incompatible`, `unsafe`, `unavailable`, `capacity`. Read-only status/stats report bounded schema/count/size information and no source scan; check header/schema and bounded SQLite `quick_check` for explicit status, not on each lookup. Ordinary connections validate schema at open and validate each addressed row.
2. Establish explicit migration registry and fixture for v1: same application ID and v2 layout except `access_sequence` on entries is absent and there is no eviction index. Recognize only the exact v1 schema and matching checkout metadata; on the first enabled storage write under exclusive lease apply transactional `ALTER TABLE`, deterministic access sequence initialization from `(kind,key,proof)`, create index, advance metadata sequence and set `user_version=2`. Preserve compatible payloads or evict to current limits; do not touch unknown/future schema. Read-only status reports v1 as compatible with migration pending. Roll back on failure; continue uncached.
3. `clear()` removes only validated fixed data leaves under the exclusive lease and retains the lock. `rebuild(seed, revalidate=...)` validates leaves, creates one private pending DB, calls the bounded seed writer, checks integrity/namespace/schema, records a rebuild, closes/fsyncs pending, calls `revalidate` immediately before atomically replacing the live DB, then fsyncs parent. Refuse unsafe leaves, unrecognized live schema or ambiguous journal state; for a positively identified corrupt database (including unreadable header at the validated dedicated path), recover by explicit clear/rebuild. If a hot journal exists, allow SQLite recovery only for recognizable healthy schema at validated private leaves; otherwise require explicit clear. Do not publish on failed seed, failed pre-publication sync, or unstable source; cleanup pending files safely. A failure after replace reports publication uncertain and instructs caller to recheck status.
4. Keep counters per generation; clear resets them, rebuild initializes counters for the new generation including `rebuild=1`. Never recursively delete, follow links, touch `wl index` storage, or auto-rebuild corruption on read.

### Required Interfaces and Contracts

- `seed` is a trusted offline callback over inputs prepared **before** the exclusive cache lease; no CodeIndex API or other cache namespace access is allowed inside it. No consumer calls maintenance implicitly. Confirmed clear returns `missing` when there is no database, but still creates/retains the private epoch record and advances it to fence writers that observed an absent cache. Enforce lease discipline for temp and live DBs; don't acquire a second namespace lease from inside seed.

### Requirement Evidence

Original Request 11–15, 17, 19; `.ai-factory/ARCHITECTURE.md` CodeIndex external disposable storage. Healthy, corrupt, known-old and unknown-version behavior differ explicitly.

### Error Handling and Logging

INFO for migration/rebuild/clear outcome; WARN for failed migration, corruption, or uncertain publication; no identifiers except bounded enum names and schema numbers.

### Tests

`test_service.py`: actual v1 fixture/migration and rollback, unknown version untouched, corrupt database explicit recovery, failed seeding and competing reader, torn epoch and confirmed recovery, unsafe leaves, namespace-local clear, and status/stats read-only.

### Acceptance Criteria

A failed rebuild cannot replace a good generation before publication; a corrupt generation is recoverable without product-state or index changes.

### Verification

`uv run pytest tests/context_cache tests/code_index` — all pass.

## Phase Risks and Mitigations

- Stale or oversized DBs must not buy authority or time: exact fresh proof, row-size gates, defensive connection settings, bounded lock retries and live fallback.

## Phase Completion Checklist

All three tasks and their commands pass; update only their checkboxes in `index.md`.
