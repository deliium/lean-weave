[← Architecture](architecture.md) · [CLI](cli.md) · [Context packing](context-packing.md) · [Benchmarks](benchmarks.md) · [Back to README](../README.md)

# Task-Aware Context Retrieval

Offline hybrid ranking over a strict admitted source snapshot. No embeddings,
vector databases, or LLM reranking. Stage `ContextProvider.build` behavior is
unchanged; retrieval is an explicit preview API and CLI surface.

Token-aware packing composes these ranked candidates into budgeted prompts; it
is a separate opt-in surface with its own contracts and session semantics. See
[context-packing.md](context-packing.md). Retrieval continues to return
references and costs only, never prompt bodies.

## Summary

`ContextRetriever` scans supported text once, optionally attaches a checked
index graph and preview Git path flags, then ranks files and definitions with
explainable integer signals.

## Usage

```bash
# Legacy literal search (unchanged)
uv run wl context search "banner"

# Hybrid retrieval
uv run wl context search "fix banner" --task-aware
uv run wl context rank "fix banner" --task-kind bugfix --kind symbol --top-k 5 --json
uv run wl context explain "why does route fail" --task-kind diagnose
```

Programmatic preview:

```python
from lean_weave.app import build_app_context, create_retrieval_provider
from lean_weave.context.retrieval_models import RetrievalRequest

provider = create_retrieval_provider(build_app_context())
result = provider.retrieve(RetrievalRequest(query="login", task_kind="bugfix"))
```

## Configuration

Nested under `context.retrieval` in `.lean-weave/config.yaml`:

| Field | Default | Notes |
|-------|---------|-------|
| `top_k` | `10` | 1–100 |
| `weights.exact_symbol` | `36` | At least one of exact/name/lexical must be > 0 |
| `weights.name` | `24` | |
| `weights.lexical` | `24` | |
| `weights.import_dependency` | `8` | Checked graph only |
| `weights.reverse_dependency` | `8` | |
| `weights.git_diff` | `5` | Preview boost only |
| `weights.related_test` | `8` | Filename conventions |
| `weights.map_importance` | `3` | Degree boost only |

YAML may override individual weights; missing keys keep defaults.

## Ranking signals

Normalized values are integers `0..1000`. Effective weight = configured weight ×
task multiplier (`bugfix` doubles lexical/git, `refactor` doubles both import
directions, `test` doubles related_test/reverse, `diagnose` doubles
lexical/related_test). Score is
`sum(value*weight) // sum(all effective weights)`.

Reasons list only nonzero weighted contributions in fixed signal order.

## Source windows and cost

- Lexical file hits: best anchor ±3 lines
- Other files: first five lines
- Definitions: up to 40 lines, clipped by `min(12000, per_file_chars_max)`
- `estimated_token_cost` uses the project conservative UTF-8 estimator on the
  exact LF-joined window (empty files cost `0`)
- Results never include source bodies

## Degradation

Stable codes (each at most once, fixed order):
`source_scan_incomplete`, `source_changed`, `source_unavailable`,
`index_missing`, `index_stale`, `index_incomplete`, `index_unavailable`,
`lexical_python_fallback`, `git_unavailable`.

Incomplete scans skip graph/Git deliberately. Missing/stale indexes fall back
to live AST facts without graph authority.

## Evaluation

```bash
# Installed deterministic retrieval suite
wl benchmark run --suite retrieval --output /secure/results/retrieval.json

# Legacy developer comparison (historical report shape)
uv run --locked python -m benchmarks.retrieval --json
```

Canonical fixtures and labels are packaged under
`src/lean_weave/benchmark/fixtures/`; `benchmarks/fixtures/` is a compatibility
wrapper for existing developer imports. Each case may carry `relevant_paths`,
inclusive `relevant_ranges`, distinct `required_dependencies`, symbol labels and
expected locations. Labels are validated against materialized source/line
counts before evaluation; stale paths/ranges and duplicate IDs are rejected.
Oracle programs, intended edits and labels never enter a candidate tree.

The installed suite reports ranked file/symbol P@K and R@K (K = 1, 3, 5, 10)
separately from actual packed-source coverage:

- **V1 source excerpts** — `ContextBuilder` at `ExpansionLevel.RANGES`
- **V2 admitted fragments** — actual packed fragments, not guessed prefixes

Source coverage unions selected intervals per path, intersects them with the
merged judged intervals, and counts tokens once with the same pinned
`conservative_estimate` in both arms. `missing_dependency_count` counts required
dependency ranges not fully covered. Rows, map output and omitted text never
satisfy source coverage. A case with no symbol labels reports symbol metrics as
`null`/not applicable, not zero. Fixed variants are `v1_default`, `v2_default`,
`v2_map_off`, `v2_no_graph`, `v2_tight`, `v2_cache_cold`, `v2_cache_warm` and
`v2_cache_disabled`; cold/warm use a private cache namespace and a warm arm
performs one unmeasured identical prime. Local cache state is not an API token
saving.

The older developer report still includes V1 prompt excerpts and its prefix-cost
proxy; those historical fields are not part of the installed comparison contract.

Representative cold/warm vs V1 file recall from a local locked run (stock
weights; regenerate with the command above — do not treat as a CI gate):

| Split | Engine | R@1 | R@3 | R@10 |
|-------|--------|-----|-----|------|
| development | v1 | 0.40 | 0.70 | 0.70 |
| development | v2_cold / v2_warm | 0.60 | 1.00 | 1.00 |
| holdout | v1 | 0.67 | 0.83 | 0.83 |
| holdout | v2_cold / v2_warm | 0.67 | 0.83 | 1.00 |

Avoid tuning exclusively against holdout trees; prefer the JSON report when
comparing changes.

### Git-only dirty-checkout comparison

Separately, `dev_auth` is committed, then comment-only edits land on the labeled
`auth/session.py` and unrelated `decoy.py`. One index build is reused for
`read_diff=None` vs the production staged/unstaged path adapter. Expected:

- labeled path receives a `git_diff` reason when the production adapter is used
- decoy stays unqualified (no `git_diff`) despite also being dirty
- rank/metric deltas are reported under `git_signal`, never mixed into the
  matched V1 baseline

### Observed limitations

1. **Static imports only** — unresolved/ambiguous imports never invent edges;
   reverse-dependency cases need an indexed resolved relation.
2. **Case-sensitive lexical matching** — queries that differ only by case from
   identifiers may rely on name/exact signals instead.
3. Designed holdout probes that did not reproduce a regression are still kept as
   labeled cases; do not tune exclusively against holdout trees.
4. **Linked-worktree Git** — gitfile / non-direct `.git` layouts yield
   `git_unavailable` rather than inventing diff evidence.

### Adding cases

Edit `src/lean_weave/benchmark/fixtures/retrieval/cases.json` (and the
corresponding `benchmarks/fixtures/retrieval/` wrapper mirror when keeping
developer imports identical). Freeze `relevant_paths`, inclusive
`relevant_ranges`, `required_dependencies` and symbol labels before inspecting
rank output. Every label is validated against the packaged tree.

## See also

- [CLI Reference](cli.md)
- [Architecture](architecture.md)
- [Code Index](code-index.md)
- [Context cache](context-cache.md) — opt-in reuse of ranking projections
- [Benchmarks](benchmarks.md)
