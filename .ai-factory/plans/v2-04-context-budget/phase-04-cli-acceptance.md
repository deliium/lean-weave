# Phase 4: Offline CLI and acceptance

Plan: [index.md](index.md)  
Tasks: 7–8  
Depends on: Phase 1 / Task 3 for Task 7; Phase 3 / Task 6 and Task 7 for Task 8

## Objective

Expose the same composition as build, inspection and budget views while preserving all legacy CLI invocations by default.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/cli/commands/context.py` | `build_command`, `inspect_command`, `_emit`, `_escape` | Build already takes a task; inspect already takes a project-relative path and has path/symbol/range views. |
| `src/lean_weave/app.py` | `create_retrieval_provider`, `_protected_index_paths` | Reuse protected-config/repository and graph wiring; no scanning during construction/help. |
| `src/lean_weave/config/loader.py`, `cli/commands/config.py` | Product YAML loading/show | Old files remain valid; initialization serializes model defaults. |
| `tests/cli/test_context.py`, `tests/cli/test_context_retrieval.py`, `tests/repository_map/test_context_preview.py` | Legacy JSON, aliases and indexed-map tests | Regression gate. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/app.py`, `cli/commands/context.py` | modify | Lazy packer factory and explicit preview selection. |
| `tests/cli/test_context_packing.py` | create | CLI routing, parity, projections, compatibility and real artifact. |
| `docs/context-packing.md` | create | Product settings, API and limitations. |
| `docs/cli.md`, `docs/getting-started.md`, `docs/context-retrieval.md`, `docs/architecture.md` | modify | Commands, examples and boundaries. |

## Task 7: Expose build inspect and budget views

### Intent
Deliver all three query views without guessing whether a positional operand names a task or a file.

### Implementation Steps
1. Add lazy `app.py::create_context_packer(context)`, reusing the strict retrieval repository, protected alternate-config path and scan-bound graph factory. Do not initialize a provider, cache, network client or persistence merely for help/factory construction. Context packing preview never maintains the code index.
2. Treat `--packed` or explicit `--pack-level` as a packed signal; treat `--legacy` or an explicit legacy-only option as a legacy signal. If both signals are present exit 2 before source/cache I/O; if exactly one is present it wins; otherwise use `context.packing.cli_default` (false preserves existing output). Legacy-only selectors are `--level`, `--path`, `--reason`, `--indexed-map`, `--symbol` and `--lines` on their respective commands; use Click’s parameter source to detect an explicitly passed default. `--json` is neutral. Never infer mode from whether a path exists.
3. Add `budget QUERY` as V2-only. All three packed views accept `--pack-level l0|l1` (default l1) and `--json`; setting `--pack-level` makes build/inspect explicitly packed unless contradicted by legacy settings. Bind all three through one helper: validate `RetrievalRequest`, use task spec = query, fixed safe preview instructions, default configured model, no tools/replay, and configured output reservation. Do not clamp the packing reservation to the gateway; preview reports its configured value and transport may later refuse an incompatible cap. CLI has no persisted expansion session at this milestone; expansion is the typed API.
4. Emit one stable JSON object containing schema version, command, status, composition and safe omission/degradation codes. Build adds renderable source bodies/map rows; inspect adds provenance/reasons/IDs and map metadata but no bodies; budget adds numeric limits and metrics only, no paths/model input. Equality of accounting across views is required. Use existing terminal escaping helpers for human output; maintain JSON Unicode source fidelity. A typed insufficient result is a successful offline evaluation (exit 0); stale or scan operational failure yields the typed safe result and exit 1; usage/config errors use existing exit 2/3. Stdout contains only the requested projection; never emit a second error object.

### Required Interfaces and Contracts
- `wl` and `lean-weave` remain aliases. `--config` is a root option. `.lean-weave/config.yaml` opt-in does not change stage `ContextProvider` behavior.
- Legacy default paths, selector validation, JSON fields and indexed-map branch remain unchanged; `--legacy` always provides access to path inspection even when V2 default is enabled.

### Requirement Evidence
Original Request CLI and V1 acceptance; existing `build_command` and `inspect_command`; `.ai-factory/ARCHITECTURE.md` CLI and retrieval boundaries.

### Error Handling and Logging
INFO safe view, mode, status and counts; DEBUG numeric breakdown only, no source, selectors or task text. Invalid selector combinations fail before source/cache I/O.

### Tests
Default and opt-in dispatch matrix, explicit legacy options (including `--level symbols`), invalid mixtures, both installed aliases, no-key/offline help, body-free inspect/budget, identical counts on all views, sanitized stdout/stderr and return codes, and no cache writes. Copy the already tracked `src/lean_weave/context/python_index.py` unchanged to that same relative path in an isolated preview checkout and query `extract_python_facts`. Pin generated packing budgets, `top_k`, scan limits and XDG cache root; require `ready`, the exact definition's path and canonical digest in inspect, its unchanged source in build, and the same numeric accounting with no paths in budget. No Git state, index generation, or uncontrolled repository growth in this test.

### Acceptance Criteria
All three query views work explicitly and in opt-in positional mode; existing positional behavior stays unchanged with existing config.

### Verification
`uv run --locked pytest tests/cli/test_context_packing.py tests/cli/test_context.py tests/cli/test_context_retrieval.py tests/repository_map/test_context_preview.py` — dispatch matrix and legacy snapshots pass.

## Task 8: Verify compatibility and document rollout

### Intent
Ship an opt-in contract that callers can use without reconstructing the budget, freshness, or delivery rules.

### Implementation Steps
1. Under the mandatory `/aif-docs` checkpoint, document strict config defaults, unit estimators vs provider billing, L0/L1/L2 examples, exact CLI opt-in/rollback, no automatic stage activation, offline expansion and task inventory semantics, expected insufficiency, overlap and stale outcomes, and the separate explicitly authorized DeepSeek adapter. Document that partial scans are marked and freshness is a point-in-time guarantee; tool results and replay must be counted in the real request, and server-side caching or memory is not assumed.
2. In existing compatibility suites prove stage `ContextProvider.build` never invokes packing/retrieval preview, existing stage messages remain identical for fixtures, and the V1 default CLI/indexed preview stays unchanged. Rerun the full default offline suite and `uv run --locked python -m compileall -q src/lean_weave`. Use Task 7's copied tracked artifact to validate installed CLI output and composition parity independent of growth.
3. Verify aggregate efficiency with the Phase 3 controlled follow-up test: count both calls' actual composed inputs (including schema/replay), require less total than deliberate replay-all, and confirm essential source survives. Treat this as a deterministic comparison, not a live benchmark promise. Rollback by omitting packed flags or setting `context.packing.cli_default: false`; no migration or cleanup required.

### Required Interfaces and Contracts
- Docs must report offline composition as local, not billed. No new network default, secret persistence, embeddings, summaries, vector database, LangGraph or autonomous exploration.
- Existing project test policy remains offline unless explicitly opted in; the implementation requires no live provider test.

### Requirement Evidence
Original Request Testing, Acceptance criteria and Out of scope; `.ai-factory/RULES.md` deterministic verification and offline tests.

### Error Handling and Logging
Document actionable reasons for insufficient/stale results and estimator fallback. No added production logging; confirm verbose logging never emits body, query, tools, secrets, or replay.

### Tests
Extend `tests/context/test_provider.py` and `tests/context/test_retrieval_provider.py` with opt-in isolation tripwires as needed; run all phase tests and the full suite.

### Acceptance Criteria
Documentation reflects tested behavior; default V1 interfaces remain unchanged and the explicit API/CLI/MockTransport paths are verified end to end.

### Verification
`uv run --locked pytest`; `uv run --locked python -m compileall -q src/lean_weave`; `uv run --locked pytest tests/cli/test_context_packing.py` — tests and compilation pass, including real-file provenance and view parity.

## Phase Risks and Mitigations

- `inspect PATH` and `inspect QUERY` are intrinsically ambiguous: dispatch only by explicit mode/config/legacy selectors, never by filesystem heuristics.
- Local efficiency comparisons do not prove API charges: document the limit and keep MockTransport and reported counters distinct.

## Phase Completion Checklist

- Tasks 7–8 satisfy their acceptance criteria and verification passes.
- Complete the mandatory docs checkpoint; update progress only in `index.md`.
