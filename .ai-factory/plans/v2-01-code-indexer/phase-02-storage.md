# Phase 2: Isolated SQLite storage

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Tasks 1–2 for Task 4; Task 3 has no prerequisite.

## Objective

Persist one replaceable, integrity-checked current source generation per checkout without coupling it to workflow checkpoints or Git state.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/workflow/state.py` | `workspace_identity`, `JsonCheckpointStore._lease`, `_write` | Production state is versioned JSON with a single-writer lease; reuse containment/error patterns, not checkpoint storage. |
| `spikes/langgraph_probe/probe.py`, `pyproject.toml` | `SqliteSaver`, spike dependencies | SQLite experience is spike-only. Use standard-library `sqlite3`, not LangGraph or an ORM. |
| `src/lean_weave/worktree/manager.py` | `_classify_app_owned_dirt`, `_archive_state` | Unknown worktree state blocks cleanup; storing outside checkouts avoids weakening that classifier or archiving cached metadata. |
| `src/lean_weave/worktree/git.py` | `preflight_repository` | `common_dir` and a shared HEAD cannot identify a checkout. The source index needs no Git subprocess or `.git` rewrite. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/code_index/{__init__,identity,models,sqlite_store}.py` | Create | Package boundary, checkout/cache identity, typed storage contracts, SQLite schema/version lifecycle. |
| `src/lean_weave/errors.py` | Modify | `CodeIndexError` with safe, enumerated failure codes and standard application exit code. |
| `tests/code_index/{test_identity,test_sqlite_store}.py` | Create | Containment, isolation, integrity and migration checks. |

## Task 3: Establish checkout and cache boundaries

### Intent

Make the active checkout a hard boundary. Cache files must not be controlled by checkout source or written into a managed worktree.

### Implementation Steps

1. `identity.py`: take an already authorized workspace root, resolve and open it as a real directory, and SHA-256 hash canonical absolute path, `st_dev` and `st_ino` using canonical JSON. Reject nonexistent/non-directory roots. A copied or moved checkout gets a new cache namespace; distinct linked worktrees, detached HEAD and same-HEAD worktrees never share it. Unborn Git and non-Git roots use the same algorithm. Use the app’s existing authorized-root convention, not unauthenticated source paths or Git discovery to widen it.
2. Use fixed layout `${XDG_CACHE_HOME:-~/.cache}/lean-weave/code-index/<checkout-key>/index.sqlite3` with `index.lock`, `index.pending.sqlite3` (first build only) and exactly the two corresponding `-journal` names. `XDG_CACHE_HOME` is a trusted process setting; unset/empty uses the fallback, nonempty relative values fail. Require the app-owned base and checkout directories owner-only (create at `0700`), and files owner-only regular single-link (create at `0600`). Reject a cache under the authorized source root. Refuse symlink components and unsafe ownership/permissions; traverse ancestors descriptor-relative, accept only root/current-user owners and no group/other write (a root-owned sticky intermediate ancestor is permitted only when opening an existing private current-user child without following links). No YAML cache relocation or per-command path override; allow a trusted injected cache base in Python for tests only, subject to the same checks.
3. Use a stable lock inode with nonblocking leases (shared for reads, exclusive for mutations), patterned after workflow leases. Read-only missing storage does not create directories/lock. If a lock already exists, readers acquire it before classifying the live DB or sidecars; a live DB without the lock is unsafe, not `missing`. Hold the lease through SQLite connection closure or through clear. Because SQLite opens its own database and journal paths, use only validated private parents, classify all fixed leaves with no-follow stat (including dangling links), and refuse if the OS cannot guarantee the required directory/file/no-follow/lock primitives. An unexpected `index.sqlite3-wal`/`index.sqlite3-shm` or pending equivalent is unsafe and not opened or removed; SQLite operates only in rollback-journal mode. Never remove the lock or directory in `clear`.

### Required Interfaces and Contracts

- `CheckoutIdentity(key: str)` and `IndexLocation` internal; `resolve_checkout(root: Path)` and `index_location(identity, *, cache_base=None)` open no DB, run no Git and are side-effect free. Lazy wiring must not perform either on CLI help or provider construction.
- Threat model: prevent source-controlled paths, symlinks, other-user writes, accidental corruption and races between cooperating index operations. A malicious process with the same user privileges is not a separate security principal. No workflow state, Git metadata, Git objects or remotes are touched.

### Requirement Evidence

Request 9 and 14–15; reconciliation row **Checkout and revision**; architecture `worktree/` boundary; product Git restrictions in project rules.

### Error Handling and Logging

Use `unsafe_cache`, `root_unavailable`, `busy` and `storage_unavailable` codes. DEBUG operation/lease outcome and WARN only for refusals; omit cache/home paths, root identity inputs and raw exceptions.

### Tests

`tests/code_index/test_identity.py`: separate roots and same-HEAD linked worktrees, changed cwd spelling resolving to one approved root, non-Git root, copied cache identity mismatch (also checked by Task 4), unsafe cache under source, symlink and mode rejection, file hardlink rejection, shared/exclusive contention, unsupported primitives, missing read leaves filesystem untouched. Test injected cache paths with private ancestors or an isolated test HOME.

### Acceptance Criteria

No cross-checkout reuse or writes in source, Git or workflow state; a contested lease fails predictably.

### Verification

`uv run --locked pytest tests/code_index/test_identity.py tests/worktree/test_git.py`.

## Task 4: Define schema validation and safe migrations

### Intent

Ensure readers see only a validated committed generation and never silently erase an incompatible or corrupted cache.

### Implementation Steps

1. Create schema version 1 with a fixed application ID (`0x4C574958`, LWIX) and `PRAGMA user_version=1`. Define `meta(singleton, checkout_key, revision, policy_key, extractor_key, manifest_sha256)`, `files(file_id PRIMARY KEY, path UNIQUE, module_name, digest, size_bytes, line_count, parse_status, facts_sha256)`, `symbols(file_id, ordinal, local_name, simple_name, kind, parent, signature, decorators_json, start_line, end_line)`, and `imports(file_id, ordinal, kind, module, level, names_json, start_line, end_line)`. Child tables have `(file_id, ordinal)` primary keys and foreign keys with cascade; add binary-collated indexes on module/symbol simple/local names and file digest. Validate `module_name` against the existing path projection. Use CHECKs for fixed enums, nonnegative values, and coherent ranges, plus typed row validation for rules involving the parent row. The store’s `ordinal` is whole-file sorted row order; symbol IDs use the per-name occurrence in Task 2, not the DB row ordinal.
2. Use connection-local `foreign_keys=ON`, `trusted_schema=OFF`, `temp_store=MEMORY`, constrained SQLite limits, parameterized statements, no extension loading, DELETE journaling, `synchronous=FULL` and `secure_delete=ON` on writer connections. Give readers an explicit read transaction using `mode=ro` and `query_only=ON`; do not use `immutable=1`. Mutation and migration use a single `BEGIN IMMEDIATE` transaction with explicit commit/rollback, not `executescript`’s implicit commit. Keep at most one generation; no cached source bodies, historical snapshots, or resolved import edges.
3. Gate reads on expected application ID, rollback-journal format, exact supported schema (including actual allowlisted tables/indexes; no foreign views/triggers), checkout key and valid metadata. Admin loads run `quick_check`, `foreign_key_check`, validate all hydrated rows and verify per-file facts hashes and a manifest checksum binding `(schema_version, checkout_key, revision, policy_key, extractor_key)` to ordered complete file records. Define `source_revision(policy_key, path_digests)` in `code_index/models.py`: SHA-256 of the tagged array `["lw-index-source-v1", policy_key, sorted_pairs]`. Task 5 consumes this helper; recompute the stored revision with it, treating a disagreement as `corrupt`, not merely `stale`. Use compact ordered UTF-8 JSON and SHA-256, exclude the checksum field itself, and retain `FileSnapshot.digest`'s `sha256:` prefix. Scoped cache readers instead check header/schema, the current snapshot and loaded file facts checksum; no whole manifest load. These checks detect accidental corruption, not deliberately forged caches from the same user.
4. Add an ordered `N -> N+1` migration registry. There is no historical index schema in this release; unrecognized/untagged files are not v0. Under the exclusive lease, for an existing live DB first refuse any live rollback journal (`recovery_required`) and inspect the DB with `mode=ro`; fully validate the current or explicitly registered older format before reopening `mode=rw`. Do not run any writable PRAGMA before this gate. Only then run a registered migration and publish inside the same transaction; validate new schema/integrity before commit. A crash cannot expose partial rows to readers; if a live journal remains, do not let an open silently recover it or mark it current. This disposable cache requires confirmed clear/rebuild in that case. Test the migration runner with a test-only supported schema/migration and an induced rollback. Reject unknown older/newer versions without changes.

### Required Interfaces and Contracts

- `SqliteIndexStore` is internal; context-managed `read()` and `write()` transaction entry points, typed manifest/header access, parameterized per-file fact loading and reconciliation writes. No library method receives raw SQL from a caller. Database health outcomes are `missing`, `healthy`, `incompatible`, `corrupt`, or `unavailable`; healthy is a storage property, not a freshness claim.
- Scope validation uses Task 1’s `validate_index_source_path()` and rejects invalid type/range/ordinal, unknown statuses, disallowed credential canaries in loaded fields, and inconsistent checksum. Match stored relative path, canonical digest and line count before reuse; raw byte size is accounting, not the canonical match key.

### Requirement Evidence

Request 1–2, 7, 11–12 and 15; reconciliation rows **Store lifecycle**, **Cache reuse**. ADR-002 and production `JsonCheckpointStore` constrain placement, not index schema.

### Error Handling and Logging

DEBUG schema version/transaction outcome and counts; WARN safe `corrupt`, `incompatible`, `busy` or `recovery_required` reasons. Never log SQLite dump, SQL bound values, fact values, filenames or underlying exceptions. Build/update refuse to overwrite invalid DBs; confirmed `clear` is an explicit recovery operation.

### Tests

`tests/code_index/test_sqlite_store.py`: healthy reopen, no accidental creation on read, changed header revision with intact rows, checksum mutation with structurally valid SQL, malformed/truncated file, wrong application/version/checkout, bogus schema or trigger, broken foreign key, transaction exception mid-write and mid-migration, successful test-only migration, reader/writer contention, and journal-required refusal for both readers and writers. Assert unsafe/unknown DBs and their sidecars remain byte-identical after rejected update; include unexpected WAL/SHM and dangling-sidecar cases.

### Acceptance Criteria

Corrupt data is never returned as a hit; supported upgrades are all-or-nothing; unsupported stores are unchanged by reads and rejected writes.

### Verification

`uv run --locked pytest tests/code_index/test_sqlite_store.py tests/workflow/test_state.py`.

## Phase Risks and Mitigations

- Cache placement outside the checkout avoids both Git write authority and managed-worktree cleanup changes. A removed checkout’s cache may remain; document clearing before removal and the cache’s disposable nature.
- Schema and extractor versions are independent: structural migration does not by itself invalidate AST facts; an extractor-key change does.

## Phase Completion Checklist

- Tasks 3–4 meet their acceptance criteria and verification passes.
- Update their checkboxes only in `index.md`.
