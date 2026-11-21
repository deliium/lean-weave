[← Code index](code-index.md) · [Back to README](../README.md) · [Architecture →](architecture.md)

# Repository Map

Compact, deterministic repository maps built from a **checked** persistent code
index. Map commands never call an LLM, never reparse unchanged sources, and
never update the index automatically.

## Prerequisites

```bash
wl index build          # once per checkout (or after clear)
wl index update         # after edits or switches
wl index status         # health + freshness
```

Map queries refuse missing or stale indexes. Refresh with the index commands
above; map reads never write the cache.

## Commands

```bash
wl map show
wl map module <module>
wl map dependencies <module>
wl map references <symbol>
```

Both `wl` and `lean-weave` expose the same group. Output is the budgeted text
only (no success banner). Empty evidence prints nothing and exits `0`.

| Command | Meaning |
|---------|---------|
| `show` | Globally ranked public modules and symbols |
| `module` | Selected module first, then one-hop neighbors |
| `dependencies` | Direct out/in import evidence (including unresolved/ambiguous) |
| `references` | Definition ranges plus static import sites only |

### Selectors

- Module selectors are exact indexed module names with a unique file identity.
- Symbol selectors are fully qualified names when dotted, otherwise exact simple
  names. Repeated occurrences of one `(file, local name, kind)` form one group;
  multiple groups are `ambiguous_selector`.
- Invalid syntax exits `2` with `Error: invalid_selector` and no stdout.
- Unknown or ambiguous selectors exit `1` with a fixed code and no stdout.

## Settings

Configured under `context` in `.lean-weave/config.yaml` (defaults apply when
absent):

| Key | Default | Range |
|-----|---------|-------|
| `map_tokens_max` | `1024` | 1–8192 |
| `map_rows_max` | `80` | 1–500 |

Token estimates charge **UTF-8 bytes of emitted text** (including final
newlines), one byte per estimated token. This is a local size metric, not a
provider tokenizer guarantee.

When a budget drops or shortens a row, `truncated` is true. A trailing `...`
line may be appended when it fits. If every evidence row is dropped but
truncation occurred, a single `.` (no newline) distinguishes that case from a
genuinely empty view.

## Opt-in context preview

```bash
wl context build "preview the map" --level map --indexed-map --json
wl context build "scoped" --level map --indexed-map --path src/pkg/mod.py --json
```

`--indexed-map` requires `--level map`. The preview wraps map lines in the
existing untrusted delimiters, admits complete rows under the stage prompt
budget, and does not change default context builds or workflow stages.

## Semantics operators should expect

- Python `.py` / `.pyi` only; `src/` path prefixes are stripped in indexed module
  names. Importing `src.config` is **not** rewritten to `config`.
- Public visibility: names whose components do not start with `_`; module-level
  classes/functions and class methods. No `__all__` inference.
- Ranking uses distinct incoming/outgoing import counts, then module path — not
  PageRank or other iterative graph algorithms. Raw static import evidence lacks
  runtime semantics; a heavier dependency is deferred until measured relevance
  needs it.
- References are definitions plus explicit import sites — not attribute uses or
  call graphs.
- Unresolved and ambiguous edges stay labeled; they are data, not CLI errors.
- Module views expand one hop; indexed context scopes **intersect** filters and
  do not expand neighbors.
- Guarantees apply to the checked index snapshot after live admission hashing,
  not to live editor buffers a moment later.

## Failure / remediation

| Code | Exit | Remedy |
|------|------|--------|
| `missing` | 1 | `wl index build` |
| `stale` | 1 | `wl index update` |
| `busy` / unstable storage | 1 | Retry when idle |
| `corrupt` / `incompatible` / `recovery_required` | 1 | `wl index clear --yes` then `wl index build` |
| `invalid_selector` | 2 | Fix the selector syntax |
| `not_found` / `ambiguous_selector` | 1 | Disambiguate or pick an indexed name |

## Examples

```bash
wl index build
wl map show
wl map module calc
wl map references calc.add
wl map dependencies app
```

## See also

- [Code index](code-index.md) — cache lifecycle and isolation
- [Architecture](architecture.md) — composition with ContextProvider
- [Getting Started](getting-started.md) — install and offline checks
