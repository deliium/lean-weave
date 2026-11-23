[← Architecture](architecture.md) · [CLI](cli.md) · [Context retrieval](context-retrieval.md) · [Code index](code-index.md) · [Back to README](../README.md)

# Context Cache

`context_cache/` is an **opt-in, checkout-local** SQLite cache for deterministic
repository analysis and context-construction artifacts. It makes repeated
implement/retry work cheaper on the local machine. It never calls a model,
never caches model output, and does not by itself reduce transmitted tokens.

## Summary

- Stores bounded metadata only: admitted Python facts, import relations,
  repository-map row fragments, retrieval candidates/ranking projections, and
  packing spans.
- Never stores source bodies, full prompts, raw tasks, model responses,
  workflow state, receipts, or L2 session state.
- Every lookup supplies a fresh logical key **and** a live proof. Stale or
  mismatched rows are rejected; they are never used as source or prompt
  authority.
- Disabled by default. With caching disabled, every `ContextProvider`,
  retrieval, map, and packing path keeps its pre-cache behavior.
- The legacy code index remains a separate namespace and contract. Cache
  maintenance never builds, updates, or clears `wl index` data.

## Configuration

Add this to `.lean-weave/config.yaml` (the product config, not AI Factory
config):

```yaml
context:
  cache:
    enabled: true
    max_bytes: 16777216      # 1 MiB .. 64 MiB, per checkout
    max_entries: 4096        # 1 .. 16384, per checkout
```

There is no path override, peer-discovery option, cross-worktree flag, or
automatic-rebuild setting. Existing configs keep working with `enabled: false`.

## CLI

All four commands are offline and require no `DEEPSEEK_API_KEY`.

```bash
uv run wl cache status
uv run wl cache stats
uv run wl cache clear --yes
uv run wl cache rebuild
```

| Command | Behavior | Exit codes |
|---|---|---|
| `status` | Enabled state, health, schema, entries, DB bytes, limits, migration pending | `0` for disabled/missing/ready; `1` for unsafe/corrupt/incompatible/busy/capacity/unavailable |
| `stats` | Fixed ordered totals plus per-kind counters | Same as `status` |
| `clear --yes` | Removes only this checkout's context-cache data; works after disabling | `0` for cleared/missing; `2` without `--yes`; `1` on unsafe/IO failure |
| `rebuild` | Seeds a fresh generation from one strict scan | `0` on success; `2` when disabled; `1` on incomplete/stale/corrupt conditions |

`status` and `stats` are read-only. `clear` and `rebuild` never touch the code
index or another checkout.

## Storage and bounds

```text
${XDG_CACHE_HOME:-~/.cache}/lean-weave/context-cache/<checkout-key>/
  cache.sqlite3
  cache.lock
  cache.pending.sqlite3
```

The key is derived from the resolved checkout path, device, and inode. Managed
worktrees therefore start cold unless an explicit trusted peer API is used.
The directory tree is validated as owner-only and symlink-free at use time.

Limits are per checkout, not global. The cache enforces:

- `max_entries` rows and a conservative logical byte target;
- a 256 KiB maximum for one payload;
- a SQLite page budget derived from `max_bytes`;
- least-recently-used eviction under capacity pressure.

Rollback journals and one pending rebuild generation are transient overhead
outside the logical byte target. SQLite may keep pages after eviction, so
`db_bytes` can exceed the logical payload total while still respecting the
configured page budget.

## What is reused

| Artifact | Reused when | Invalidated by |
|---|---|---|
| Python facts | Same path, digest, line count, extractor, policy | Source edit, policy/extractor change |
| Import relations | Same importer facts and consulted module buckets | Importer edit, target add/remove/shadow |
| Map row fragments | Same module/symbol identity, digest, render contract | Module or definition edit |
| Retrieval ranking | Same task inputs, file digests, graph, Git paths, settings | Query/scope/config/source/graph change |
| Packing spans | Same path digest and span contract | Source edit or parser contract change |

Budgets, visibility, symbol deduplication, import popularity, selected module,
scope filtering, lexical backend reporting, Git signals, and final witness
checks remain live. A warm map or pack can reuse rows, but it still evaluates
the current budget and current source revisions.

Opt-in dependency coverage reuses the same path-local facts and packing spans.
It does **not** persist dependency links, closure traversals, ambiguity
outcomes, ready packs, or expansion receipts. Each pack rebuilds proof from the
live scoped read universe; a competing admitted module outside the emit set can
invalidate proof even when emitted fragment digests are unchanged. Sibling
worktrees keep separate cache keys, so warm proof on one worktree never
supplies foreign dependency context to another.

## Maintenance and recovery

`wl cache rebuild`:

1. Opens one complete strict retrieval scan.
2. Reads a matching checked index generation when one exists, then releases
   the index lease.
3. Takes the context-cache maintenance lease and seeds one private pending
   database.
4. Validates schema and integrity, revalidates the source scan, then atomically
   replaces the live database and advances the maintenance epoch.

A failed seed, failed validation, or changed source leaves the previous
generation intact. The index is never rebuilt automatically.

Known schema version 1 is migrated transactionally. Unknown or future schemas
are never overwritten. A positively identified corrupt database should be
recovered explicitly:

```bash
uv run wl cache clear --yes
uv run wl cache rebuild
```

## Worktree isolation

The default is strict checkout isolation: two worktrees with identical HEAD and
identical files still have separate cache keys. No ordinary CLI or workflow
command discovers or reads a peer's cache.

A trusted caller may opt into a narrow peer-facts capability through
`lean_weave.app.open_peer_fact_reuse`. It requires:

- two different **managed** worktrees of the same repository;
- matching live source identity, admission policy, and cache configuration;
- an explicit bounded Python path scope;
- successful strict scans on both sides; and
- a read-only lookup of the peer's existing facts row, followed by local
  revalidation.

Only path-local Python facts may cross that boundary. No credentials, workflow
state, graph IDs, map fragments, rankings, pack components, or source bodies are
shared. The capability is short-lived and is not exposed as a CLI option.

## Measurement

Run the offline developer benchmark:

```bash
uv run python -m benchmarks.context_cache --json
```

It compares disabled, empty-enabled, and warm runs over the checked-in
retrieval fixtures, with and without a prebuilt code index. It reports:

- parity signatures for retrieval and packed output;
- per-kind cache hits and misses;
- parser/resolver/renderer/ranking/span call counters;
- database bytes and local wall/CPU time;
- `llm_calls=0` and `api_tokens_measured=false`.

Local time and eliminated calls are **not** API token savings. The cache does
not assume that a local hit reduces transmitted tokens.

## Logging

Cache diagnostics use the configured lean-weave log level
(`LEAN_WEAVE_LOG_LEVEL`, then `LOG_LEVEL`). Logs carry only artifact kind,
event, schema number, and bounded reason codes. They never include SQL text,
database paths, task text, source payloads, or raw exception messages.

## V2 validation status

The cache workload now records every wall/CPU sample, priming time, per-kind
hits/misses, pack-build and lookup counters, and separate initial-build,
unchanged-update, edit, rename, delete, recreate and clean-rebuild timings for
the index. Measured on two tiny packaged cases: warm reuse removes counted
ranking work but is not uniformly faster than disabled mode (for example
`multi_file_banner/indexed` measured 8.66 ms warm vs 8.67 ms disabled median).
A bounded synthetic workload (`python -m benchmarks.context_cache --json --scaled`)
adds 100 and 1,000 off-scope files plus a 50-importer dense tree. Fact extraction
grows with fanout while ranking and pack work stay bounded; output signatures
remain identical. No cache-speed default was changed. See
[v2-readiness.md](v2-readiness.md).

## See also

- [code-index.md](code-index.md) — the separate disposable source index
- [context-retrieval.md](context-retrieval.md) — ranking inputs and degradation
- [context-packing.md](context-packing.md) — budgets, sessions, and delivery
- [benchmarks.md](benchmarks.md) — offline evidence and metric units
