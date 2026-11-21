[← Architecture](architecture.md) · [Back to README](../README.md) · [CLI Reference →](cli.md)

# Persistent Code Index

The code index is a **local, disposable SQLite cache** of Python source facts.
It never calls an LLM, never writes into the checkout, and never decides what a
prompt contains. Context retrieval still selects and admits source; the index
only lets it reuse a previous AST parse for an unchanged, still-admitted file.

## Command surface

All commands run offline and may be used without an API key:

```bash
wl index build          # create/refresh the current generation
wl index update         # refresh an existing generation
wl index status         # health + freshness, no parsing or writing
wl index clear --yes    # remove this checkout's cache
```

- `build` bootstraps missing storage and behaves like `update` when a generation
  already exists.
- `update` refuses with `missing` when no generation exists; run `build` first.
- `status` exits `0` for `missing` or `healthy` storage (including `stale` /
  `unknown` freshness) and `1` for `incompatible`, `corrupt`, or `unavailable`.
- `clear` without `--yes` exits `2` before any cache I/O. Confirmed clear is
  noninteractive and prints `cleared` or `missing`. It never touches source,
  workflow checkpoints, the lock, or the cache directory.

No command reads `DEEPSEEK_API_KEY`, constructs an LLM provider, contacts the
network, executes workspace code, or writes a usage ledger.

## Maintenance sequence

The operator explicitly refreshes the cache after edits or checkout switches:

```bash
wl index update
wl index status
```

There is no automatic indexer and no background service. Ordinary context
builds do not update the cache; they only perform a read-only, per-file lookup
after V1 admission. A stale entry simply falls back to the normal AST parser.

After changing branches, commits, staged edits, or unstaged edits that change
admitted bytes, run `update`. A Git metadata-only change (new commit, same
files) does not change the generation.

## Freshness, repair, and recovery

`status` reports two independent dimensions:

- **health** — `missing`, `healthy`, `incompatible`, `corrupt`, `unavailable`
- **freshness** — `current`, `stale`, `unknown` (only meaningful for healthy storage)

`unknown` means the scan could not prove a complete observed generation; the
cache is not used for whole-revision answers. Lookup and update fail closed for
non-current input rather than returning partial results.

Safe recovery sequence:

```bash
wl index clear --yes
wl index build
```

Use it for `corrupt`, `incompatible`, a leftover rollback journal
(`recovery_required`), or an interrupted first build. After
`publication_uncertain`, recheck with `wl index status` instead of retrying the
rename. After `clear_incomplete`, retry the confirmed clear. Never clear or
replace an unsafe path reported as `unsafe_cache`.

The cache is disposable, not forensic erasure: SQLite deleted-row guarantees are
not a promise that data is unrecoverable from the storage device. Do not place
credentials, tokens, or private keys in indexed source.

## Cache location and isolation

The fixed layout is:

```text
${XDG_CACHE_HOME:-~/.cache}/lean-weave/code-index/<checkout-key>/
  index.sqlite3
  index.lock
  index.pending.sqlite3        # first build only
  *.sqlite3-journal            # rollback journals
```

- `<checkout-key>` hashes the canonical checkout path, device, and inode.
  Copied or moved checkouts get a new namespace. Linked worktrees at the same
  HEAD do not share a cache.
- The cache must live outside the authorized source root. Relative
  `XDG_CACHE_HOME` values are rejected.
- App-owned directories are created `0700`; files are created `0600`. Symlinked
  components, group/other-writable directories, hardlinked files, and foreign
  ownership are refused.
- A shared lock governs reads; mutation and clear take an exclusive,
  nonblocking lease. A live database without its lock is unsafe and is not
  opened.
- SQLite runs in rollback-journal mode only; unexpected WAL/SHM files make the
  store unavailable rather than being removed automatically.
- Removing a checkout leaves its cache behind. Clear it before deleting the
  checkout if that matters; the cache never enters Git status, workflow
  checkpoints, worktree archives, or prompts.

No Git subprocess, routing record, workflow checkpoint, provider journal, or
historical source body is stored.

## What is indexed

Only canonical, policy-admitted `.py` and `.pyi` files become facts:

- module names, classes, functions, methods, async kinds, signatures,
  decorators, and inclusive 1-based decorated ranges
- per-statement imports with relative levels and ordered names/aliases
- stable source identifiers and per-name occurrence ordinals

Excluded, ignored, generated, oversized, binary, undecodable, credential-
quarantined, hardlinked, symlinked, and unsupported paths never become rows.
Nested `.gitignore` rules, additive `context.exclusions`, hard denials, and
state/cache/generated directory bans apply exactly as in V1 context previews.

UTF-8 is required. A leading BOM is removed and CRLF/CR line endings are
canonicalized before hashing, so a line-ending-only change does not trigger a
reparse. A file with invalid syntax remains module metadata with no fabricated
definitions or imports; the next update reuses that outcome until the canonical
digest or extractor changes.

### Incremental behavior

- A path with the same canonical digest and compatible extractor reuses its
  facts without parsing.
- A unique exact move preserves the file identity and facts; copies, ambiguous
  duplicates, and rename-plus-edit are remove/add.
- Delete, ignore, exclusion, and quarantine transitions remove prior rows only
  after a complete scan.
- Partial scans, changed witnesses, I/O races, and descriptor replacement abort
  publication instead of inferring removals.
- The extractor key changes with the AST extraction version and Python
  major/minor generation; a change reparses unchanged content as `invalidated`.

### Stable identifiers

Definition IDs derive from the file ID, local qualified name, kind, and the
occurrence ordinal among duplicate names ordered by range. Unrelated line
shifts and an unambiguous move keep IDs stable. Reordering duplicate
definitions can reassociate occurrence ordinals.

## Limits and platform behavior

- One current generation per checkout; no history, snapshots, resolved graph
  edges, source text, embeddings, vectors, or semantic summaries.
- A store is bounded to 64 MiB; hydration is bounded by the configured source
  and entry limits. Exceeding a bound is an error, never a partial commit.
- Strict scanning requires directory descriptors, no-follow opens, and
  `flock`. Unsupported platforms report strict admission unavailable rather
  than falling back to pathname checks.
- Locale is not consulted; sorting and names are byte-stable. Tree-sitter is
  not a dependency.

## Context integration

When a default `DeterministicContextProvider` build needs Python facts, it:

1. admits the file through the existing V1 context policy;
2. asks the read-only cache for facts matching the current path, canonical
   digest, policy, and extractor;
3. falls back to the existing AST parser on any miss, busy, stale, corrupt, or
   unavailable state.

Plan and verify never open the cache. Implement and retry open at most one
read-only transaction per build and keep an operation-local memo. Index
metadata, health, decorators, and revision are never added to prompts. The
ContextProvider protocol, builder ranking, `ContextPack`, V1 CLI output, and
executor tools are unchanged.

Warm cached facts produce byte-identical prompts to cold parsing for the same
admitted source; the offline acceptance test asserts this on the benchmark
fixtures.

Checked whole-generation reads also feed the offline [Repository map](repository-map.md)
(`wl map …` and optional `wl context build --indexed-map`). Those surfaces never
mutate the index and never become workflow prompt authority by default.
