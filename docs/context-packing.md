[← Context Retrieval](context-retrieval.md) · [CLI](cli.md) · [Architecture](architecture.md) · [Back to README](../README.md)

# Token-Aware Context Packing

`ContextPacker` composes ranked, revision-checked source into a prompt under
independent section and request budgets. It is an explicit, read-only preview
API first; workflow stages keep using `ContextProvider.build` unchanged.

No embeddings, vector stores, LLM summaries, autonomous exploration, or
LangGraph are involved.

## Summary

- **L0** — a compact, metadata-only repository map.
- **L1** — the required definition plus optional ranked evidence and map rows.
- **L2** — an explicit follow-up for a symbol, range, justified full file, or
  one registered dependency edge.

Packs carry exact paths, inclusive lines, canonical source digests, selection
reasons, revisions, included fragment IDs, and composition counters. A pack is
dispatchable only while its status is `ready`.

## CLI

```bash
# Legacy default (unchanged)
uv run wl context build "wire the provider seam" --path src/lean_weave/app.py
uv run wl context inspect src/lean_weave/app.py --symbol create_llm_provider

# Explicit V2 packing
uv run wl context build "wire the provider seam" --packed --json
uv run wl context inspect "wire the provider seam" --packed --json
uv run wl context build "map only" --packed --pack-level l0
uv run wl context budget "wire the provider seam" --json

# Force the legacy view even when packing is the configured default
uv run wl context build "wire the provider seam" --legacy
```

`--packed` and `--pack-level` are packed signals. `--legacy` and legacy-only
selectors (`--level`, `--path`, `--reason`, `--indexed-map`, `--symbol`,
`--lines`) are legacy signals. Conflicting explicit signals exit `2` before
source or cache I/O. When neither is present, `context.packing.cli_default`
chooses the mode.

`budget` is V2-only and prints numeric limits and counters only: no paths,
source, model input, or omissions with paths.

A typed `insufficient_context` result is a successful offline evaluation
(exit `0`). `stale_reference` and source-scan operational failures exit `1`.
Invalid flags/config use the existing exit codes.

## Configuration

Nested under `context.packing` in `.lean-weave/config.yaml`. Defaults preserve
legacy CLI behavior.

| Field | Default | Notes |
|-------|---------|-------|
| `cli_default` | `false` | `true` makes bare `build QUERY` / `inspect QUERY` packed |
| `total_tokens` | `8192` | Whole counted input plus reserved output |
| `system_tokens` | `1024` | Trusted system instructions only |
| `task_tokens` | `1024` | Task specification block |
| `map_tokens` | `512` | Repository map block; also capped by `context.map_tokens_max` |
| `source_tokens` | `4096` | Source fragments |
| `tool_tokens` | `2048` | Tool schemas, replay messages and response format |
| `output_tokens` | `1024` | Reserved completion cap; never borrowed for source |
| `max_expansions` | `2` | Recognized L2 attempts per task session (0–16) |
| `cumulative_source_tokens` | `8192` | Unique added source over the task (≥ `source_tokens`) |

Limits are independent hard caps and need not sum to `total_tokens`. Output must
be smaller than total, each input cap must fit `total_tokens - output_tokens`,
and the cumulative cap must be at least the source cap. Existing
`per_file_chars_max`, `total_code_chars_max`, `map_tokens_max`, and
`map_rows_max` still apply. No migration is required; older files keep these
defaults.

## Accounting

Local composition reports one of two estimator kinds:

- `local_tokenizer` — an explicitly injected adapter that declared support for
  the exact effective model and wire shape.
- `conservative_estimate` — one token per serialized UTF-8 byte plus fixed
  framing. This is the default because the locked runtime ships no verified
  DeepSeek tokenizer.

Section counts and the final composed request are counted separately. Tool
schemas, response format, and assistant/tool replay are counted once in the tool
section and again only as part of the whole request. The output reservation is
added after input counting.

These are **local composition counters**, not provider billing. The DeepSeek
gateway keeps its own conservative pre-dispatch reservation and settles against
API-reported counters; a local estimate is never presented as reported usage.
Server-side caching or memory is not assumed.

## Programmatic API

```python
from lean_weave.app import build_app_context, create_context_packer
from lean_weave.context.packing_models import (
    ExpansionRequest, PackRequest, SymbolSelector,
)

packer = create_context_packer(build_app_context())
request = PackRequest(
    task_id="task-1",
    query="fix the render path",
    task_specification="Fix the render path without changing JSON output.",
    system_instructions="Use only supplied source.",
    level="l1",
    required_symbols=(
        SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.Widget.render"),
    ),
)
result = packer.build(request)
assert result.status in {"ready", "insufficient_context", "stale_reference"}
```

Task-local L2 expansion:

```python
result, session = packer.open_task(request)
assert session is not None
expanded = packer.expand(
    session,
    ExpansionRequest(
        kind="symbol",
        path="pkg/core.py",
        expected_digest=session.revisions["pkg/core.py"],
        qualified_name="pkg.core.helper",
    ),
)
if expanded.receipt_id:
    delivery = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, session.receipts[expanded.receipt_id].carry_forward_ids
    )
```

`open_task` creates a process-local, single-owner session only for a ready pack.
It stores revisions, coverage, fragment references, and bounded receipts—never
source bodies, transcripts, or tool replay. Every expansion opens a fresh strict
scan, revalidates recorded revisions and the frozen admission policy, and charges
one attempt per recognized selector (including insufficient, stale, and
no-change outcomes). Invalid caller references do not consume attempts.

Covered selectors return `no_change` without dispatchable messages. A changed
source, checkout identity, or admission result invalidates the session with
`stale_reference`; there is no automatic refresh or silent rebuild. An
incomplete scan that cannot verify a recorded path returns
`insufficient_context` with `source_scan_incomplete`.

### Explicit DeepSeek consumption

The packer never calls a model. An authorized caller converts a ready pack into
the existing gateway request:

```python
from lean_weave.context.packing_gateway import prepare_packed_request

llm_request = prepare_packed_request(
    result.pack,
    feature_id="feature-1",
    stage="implement",
    outbound_paths=["pkg/core.py"],
)
response = llm_provider.complete(llm_request)
```

The adapter requires a `ready` pack, a workflow stage, and caller-authorized
outbound paths covering every fragment and map row. It forwards no pack metadata
to the SDK and attaches a local guard with the counted wire fingerprint, model,
output cap, and composition counters. The gateway validates the guard after
normalization and before client creation, ledger reservation, or transport. A
malformed guard—including an explicitly present `None`—is rejected rather than
falling back to legacy behavior.

For L2 dispatch, use `prepare_expansion_delivery` even for a standalone delta:
the caller resupplies the exact carry-forward IDs and any tool replay, and the
session rematerializes the recorded prior and delta refs from a fresh scan. The
delivery is recounted, never recharged to the cumulative source inventory, and
cannot be replayed with different IDs. The gateway does not check the checkout;
ask the packing API to revalidate or rebuild if newer source is needed.

## Outcomes and reasons

Stable statuses: `ready`, `insufficient_context`, `stale_reference`,
`no_change`. Typical reason codes include:

| Code | Meaning |
|------|---------|
| `no_relevant_source` | No positive direct hit could serve as the L1 anchor |
| `required_source_unavailable` | A required selector could not be resolved from admitted source |
| `required_source_over_budget` | Required evidence cannot fit without clipping |
| `map_unavailable` | L0 had no admissible map rows |
| `section_budget:<name>` | The named required envelope section exceeds its cap |
| `total_budget` | The counted input plus output reservation exceeds the total |
| `cumulative_budget` | Unique L2 additions exceed the task inventory cap |
| `overlap_requires_explicit_rebuild` | A partial overlap cannot be expressed as complete units |
| `invalid_structural_range` | A requested range cannot be promoted to complete structure |
| `dependency_unavailable` | The graph/edge is missing, stale, or unresolved |
| `expansion_limit` | Recognized attempts exceed `max_expansions` |
| `source_scan_incomplete` | Freshness cannot be proven from an incomplete scan |
| `source_changed` | A revalidated witness changed; the session is invalidated |

Non-ready results may expose safe metadata and omissions but never deliverable
prompt messages.

## Freshness and source integrity

- Every emitted fragment is a contiguous canonical slice of one admitted
  snapshot. Indentation, Unicode, blank lines, and EOF newline presence are
  preserved.
- Ranked windows are references, not prompt excerpts. Definitions are resolved
  to complete decorator-inclusive AST spans; ranges are promoted to complete
  statements or rejected.
- Overlapping units are deduplicated or omitted whole. A broader optional unit
  never trims a required selection; crossing mandatory units fail explicitly.
- Source bodies are never sliced to fit a budget, and full files are used only
  for an explicit, justified request.
- Scans can be marked incomplete. That marker is preserved and absent
  dependencies are not inferred.

Freshness is a point-in-time guarantee for the duration of one operation. The
packer does not maintain the code index, does not write into the checkout, and
does not follow symlinks.

## Rollback

Omit the packed flags, or set `context.packing.cli_default: false`. The V1
builder, `ContextPack`, `ContextProvider.build`, stage prompts, and workflow
checkpoints are unchanged. No cleanup or migration is required.

## See also

- [context-retrieval.md](context-retrieval.md) — ranked retrieval inputs
- [repository-map.md](repository-map.md) — checked import graph
- [code-index.md](code-index.md) — disposable fact cache
- [context-cache.md](context-cache.md) — opt-in span/header reuse
- [cli.md](cli.md) — command reference
