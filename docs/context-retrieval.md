[← Architecture](architecture.md) · [CLI](cli.md) · [Benchmarks](benchmarks.md) · [Back to README](../README.md)

# Task-Aware Context Retrieval

Offline hybrid ranking over a strict admitted source snapshot. No embeddings,
vector databases, or LLM reranking. Stage `ContextProvider.build` behavior is
unchanged; retrieval is an explicit preview API and CLI surface.

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
uv run --locked python -m benchmarks.retrieval --json
```

Compares V1 lexicographic ranking to V2 cold/warm retrieval on labeled cases
(including pinned `bug_fix` / `multi_file_feature` and synthetic
development/holdout trees). Git is disabled for the matched comparison.
Precision@K / Recall@K are reported separately from:

- **V1 prompt excerpts** — `ContextBuilder` at `ExpansionLevel.RANGES` with a
  fixed 2048-token budget (actual admitted prompt evidence, not V2 windows)
- **V2 source-cost estimates** — cumulative `estimated_token_cost` at K plus
  labeled-interval coverage under 256 and 512 token prefixes

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

Edit `benchmarks/fixtures/retrieval/cases.json` and optional trees under
`benchmarks/fixtures/retrieval/trees/`. Freeze `relevant_paths` / symbol labels
before inspecting rank output.

## See also

- [CLI Reference](cli.md)
- [Architecture](architecture.md)
- [Code Index](code-index.md)
- [Benchmarks](benchmarks.md)
