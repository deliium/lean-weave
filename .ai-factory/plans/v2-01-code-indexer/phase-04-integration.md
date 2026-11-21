# Phase 4: Context integration, command surface and offline acceptance

Plan: [index.md](index.md)  
Tasks: 7–9  
Depends on: Tasks 1–6

## Objective

Expose offline maintenance commands and reuse verified indexed facts in ordinary context builds without changing retrieval authority, prompt shape or workflow routing.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/app.py` | `AppContext`, `create_context_provider`, `create_orchestrator` | Lazy wiring, injected provider wins, explicit feature worktree root. |
| `src/lean_weave/context/provider.py` | `_repository`, `_targeted_index`, `_plan`, `_verify`, `_implement`, `_retry` | Implement has discovery and emission passes; retry only emits affected/blocker paths; plan/verify must not scan. |
| `src/lean_weave/context/builder.py`, `src/lean_weave/context/ranking.py` | `ContextBuilder`, current ranking signals | Supplied V1 `PythonIndex` controls selection; no new ranking signal or prompt metadata. |
| `src/lean_weave/cli/{app,runtime}.py`, `src/lean_weave/cli/commands/context.py` | `SafeGroup`, `command_context`, `context_app` | Register a lazy offline Typer group. Keep V1 context CLI, search and inspect intact. |
| `benchmarks/`, `docs/mvp1-readiness.md`, `tests/cli/test_feature_offline_e2e.py` | Pinned fixture / offline acceptance | Use a representative checked-in benchmark tree and existing offline verification instead of live requests. |
| `src/lean_weave/workflow/session.py`, `benchmarks/offline.py` | `FeatureSession.start` / `resume`, `worktree_for`, `invoke_cli`, `install_gateway_transport` | Review pause exposes the real workspace, making explicit CLI warm-up possible before the resumed implement stage. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/app.py`, `src/lean_weave/context/{provider,python_index}.py` | Modify | Lazy fact-loader binding and operation-local memo; preserve V1 projection. |
| `src/lean_weave/cli/commands/index.py`, `src/lean_weave/cli/app.py` | Create / modify | Index command group and command-safe reporting. |
| `tests/context/test_provider.py`, `tests/test_app_context.py`, `tests/cli/{test_index,test_feature_offline_e2e}.py`, `tests/code_index/test_offline_e2e.py` | Extend / create | Warm/cold/fallback parity, CLI/worktree behavior and representative offline acceptance. |
| `README.md`, `docs/{getting-started,architecture}.md`, `docs/code-index.md` | Modify / create | Link, explain, and operate the disposable cache; complete `/aif-docs` checkpoint. |

## Task 7: Reuse verified SQLite facts in the ContextProvider

### Intent

Save locally repeated parsing without letting the persistent index decide which files, graph edges, or metadata a prompt receives.

### Implementation Steps

1. Add an optional `fact_loader_factory` to `DeterministicContextProvider` and a keyword-only `load_facts` callback to `build_python_index`. In `app.py::create_context_provider`, after honoring injected providers, supply a lazy factory bound to the exact workspace root/config and normalized in-root selected config path. A stage-local loader opens at most one read-only transaction on first admitted Python use; close it at the end of `_targeted_index` even on exceptions. No cache path resolution, filesystem scan or DB open in the app factory, `plan` or `verify`.
2. Pass the same memoizing loader into both `_targeted_index` passes. `build_python_index` first calls the existing `repository.try_read(path)` at the existing scope, then invokes the loader with that `FileSnapshot`. Use operation-local memo keyed by `(path, digest, extractor_key)` for both store hits and fresh facts. A shared SQLite scoped read checks compatible checkout/schema/extractor/policy and matching current path/digest/line count plus row checksum. Do not require a whole-tree freshness check; current per-file bytes are the authority for these path-independent facts. On missing, busy, incompatible, `recovery_required`, corrupt or stale file, extract through Phase 1; do not write/repair or try an alternate workspace’s cache. After a session-level cache error, disable persistent reads (not the fresh parse and memo) for the remainder of that build.
3. Reproject through the **existing V1** current-inventory module mapping and import resolver on each pass. Never consult persisted neighboring paths or graph edges for membership. Keep `plan`/`verify` metadata-only, implement’s forward one-hop and tests flag, and retry’s existing affected/blocker emission scope. Do not change the ContextProvider Protocol, `FeatureState`, builder scoring, `ContextBundle`, V1 CLI outputs or executor tools; do not add imported index columns, revision, health, decorators or index error notes to prompts.

### Required Interfaces and Contracts

- `load_facts(FileSnapshot) -> PythonFacts` yields local parsed facts whether cached or extracted; `build_python_index` continues accepting calls with no callback. The optional factory is an internal/test seam, not config authorization or a public workflow flag. Only the application’s default provider uses the persistent index automatically when available.
- Current scope/admission and file digest precede SQLite hits. Whole-revision staleness from unrelated changes does not invalidate matching path-independent facts when policy and extractor match; a re-scoped current V1 index is assembled separately from the memo.

### Requirement Evidence

Request 7, 9, 12, 15–16, token policy and V1 acceptance; reconciliation rows **Cache reuse**, **Provider stages**. V1-04 — read scopes and prompt budget; V1-08 — offline workflow behavior.

### Error Handling and Logging

DEBUG aggregate scoped hit/miss/fallback counts and reason codes at build close; WARN only corruption or unsafe storage once per attempted session, not missing cache. Log neither paths nor fact payloads nor prompts. Let existing V1 admission errors flow as before.

### Tests

Extend `tests/context/test_provider.py` and `tests/test_app_context.py`: same prompt, pack provenance and budget for warm/cold/corrupt/missing cache; changed source digest reparses; duplicate stage passes parse once on fallback; matching path can be reused after unrelated edit; cache reads occur only after scope and admission; rejected paths and isolated worktrees cannot hit; no cache access at plan/verify or on lazy construction; injected provider wins; no writes from workflow.

### Acceptance Criteria

Warm scoped hits make no AST call and no prompt change; stale/rejected rows do not leak or widen scope; current V1 operations continue with no cache.

### Verification

`uv run --locked pytest tests/context/test_provider.py tests/context/test_builder.py tests/test_app_context.py tests/cli/test_context.py`.

## Task 8: Expose offline index maintenance commands

### Intent

Provide a minimal, explicit maintenance surface under the existing `wl` app.

### Implementation Steps

1. Create `cli/commands/index.py` and register `index_app` under `cli/app.py`. Implement `build`, `update`, `status`, and `clear` with `command_context(ctx)`, the authorized cwd root, and the same source configuration and protected paths as Task 7. Factor construction inputs in `app.py::create_code_index` so CLI and provider agree. A global `--config` selects settings, never the index DB path or a different root.
2. Build/update print a deterministic one-line revision/schema and count summary; status prints storage health/freshness, revision/schema and counts or `-` when unavailable; use fixed order, no raw paths or names. `status` returns 0 for missing/healthy (even stale/unknown) and 1 for incompatible/corrupt/unavailable. Mutation success returns 0; safe operational failure returns 1 via `SafeGroup`; `clear` without `--yes` returns usage code 2 before any cache I/O. Confirmed `clear --yes` is noninteractive and prints `cleared` or `missing`; never prompt or delete source/workflow data.
3. Preserve lazy nested help, both console aliases and no provider construction, network access, source execution, Git writes or usage ledger writes in any of the four commands.

### Required Interfaces and Contracts

- `wl index build`, `wl index update`, `wl index status`, `wl index clear --yes`; no symbol CLI, background service or new config fields. Stable bounded refusal hints: build a missing cache, retry a busy cache, use confirmed clear then build for corrupt/incompatible or `recovery_required`, recheck via status on `publication_uncertain`, or retry confirmed clear on `clear_incomplete`. Do not recommend clearing unsafe paths; distinguish `recovery_required` from other `unavailable` states in safe status output.
- Configuration failures retain the existing CLI handling; raw SQLite, filesystem and parser exceptions must not reach Typer error output.

### Requirement Evidence

Request CLI, 1–4, 11–15, and zero-LLM constraint; reconciliation rows **Store lifecycle**, **Checkout and revision**.

### Error Handling and Logging

Reuse `SafeGroup` and stderr configuration. INFO operation/outcome, DEBUG bounded count diagnostics, WARN reason-coded refusals. Keep safe command results on stdout; no absolute paths or source data in new diagnostics.

### Tests

Create `tests/cli/test_index.py`: invoke actual Typer groups including offline help from an invalid config, missing/update refusal, first and second build, stale status after edit, update and current status, corrupt status and safe recovery, clear confirmation/idempotence, output ordering, aliases wiring and no provider/client/ledger side effects. Use temp checkout and external private cache.

### Acceptance Criteria

All four commands work through `wl`; read/help are non-mutating and maintenance does not require a credential or network.

### Verification

`uv run --locked pytest tests/cli/test_index.py tests/cli/test_cli.py tests/cli/test_context.py` and `uv run --locked wl index --help`.

## Task 9: Validate and document the offline workflow

### Intent

Show that the real V1 fixture and worktree workflow still function with cache reuse, and document deployment and recovery without promising automatic indexing.

### Implementation Steps

1. Add a real-fixture acceptance test in `tests/code_index/test_offline_e2e.py` using `benchmarks.fixtures.materialize("bug_fix", destination)` and `benchmarks/fixtures/manifests/bug_fix.json`. Use CLI build/update/status and typed lookup of `add` in `src/calc.py`; verify range against canonical source. Check a plain origin and a linked checkout at the same HEAD have independently missing/built states; switch a disposable checkout between commits with differing Python source and verify stale→current, then clear one cache while the other remains current. Run the default ContextProvider before/after warming for the manifest task and compare canonical messages, scope and size; make the gateway fail fast if invoked by indexing.
2. Extend `tests/cli/test_feature_offline_e2e.py::test_review_persists_plan_without_patch_until_approve` for both cold and warm `single_file_feature` runs via existing benchmark helpers. Set a private external `XDG_CACHE_HOME`. After the existing review pause, resolve the feature checkout with `worktree_for`, and in the warm run invoke `wl --config <same operator config> index build` from that checkout (temporarily change cwd and restore it before `resume`); assert the planning transport's send count is unchanged and the origin cache remains missing. Resume with the real approval token, existing `ScriptedTransport`, and production provider factory. Instrument actual scoped store loads for `src/greeter.py` and the provider’s extraction seam, not all AST use (executor and verification may parse independently). Assert at least one warm provider hit with zero provider extraction for that file, a cold extraction, and equal implement-stage canonical messages/pack size for the same fixture. Keep the original success, edit and send-count assertions. No index metadata in capture outputs committed to the repository.
3. Exercise existing V1 context CLI, executor source lookup, worktree lifecycle and offline feature E2E regressions. Verify cached DB files never enter checkout status, workflow checkpoints, archives, logs or prompts. Record parse counts by assertion, not wall-clock thresholds or token claims from a live run.
4. Complete the mandatory `/aif-docs` checkpoint: link `docs/code-index.md` from README/getting-started, document architecture seam, maintenance sequence, explicit update after edit/switch, status/repair/clear (including interrupted-write recovery), cache path and worktree isolation, scope/size limits, UTF-8/BOM rules, syntax handling, stable-ID limitations, local metadata sensitivity, no forensic erase, retained caches of deleted worktrees, unsupported platform behavior and that locale/LLM/Tree-sitter are not dependencies. Document V1 fallback and that metadata does not automatically enter prompts.

### Required Interfaces and Contracts

- Offline tests use the existing no-network policy. No production code imports benchmark code. Retain the V1 baseline artifact unchanged; no new benchmark command or LLM gate.
- The docs checkpoint describes only behavior delivered by Tasks 1–8; do not edit roadmap status as part of this feature plan.

### Requirement Evidence

Request testing/acceptance and out-of-scope clauses; reconciliation rows **Checkout and revision**, **Provider stages**, **Offline representative case**. V1-08 and `docs/mvp1-readiness.md` — existing offline acceptance.

### Error Handling and Logging

Tests show reason-bearing safe failures and no raw credential canaries. Use existing debug logs when diagnosing test failures; do not add a telemetry service or log source evidence.

### Tests

Create the real-fixture index test and extend the offline review/resume test above; ensure installed `wl` help remains offline. Run the focused suites below and the complete locked test suite. No live DeepSeek calls or paid tokens.

### Acceptance Criteria

A real Python fixture passes full build, incremental update, correct source-range verification and warm/cold prompt parity; two worktrees stay isolated; V1 offline feature tests and operational docs are complete.

### Verification

`uv run --locked pytest tests/code_index tests/context tests/cli/test_index.py tests/cli/test_feature_offline_e2e.py tests/worktree tests/executor/test_source.py` then `uv run --locked pytest`.

## Phase Risks and Mitigations

- A global status check in context builds would duplicate source scanning: test that the provider performs scoped reads only and never invokes status/update.
- Cache metadata is disposable but sensitive: owner-only storage, no raw logs or auto-prompt inclusion, and documented clear/rebuild.

## Phase Completion Checklist

- Tasks 7–9 meet their acceptance criteria and offline verification passes.
- `/aif-docs` checkpoint is complete. Update task checkboxes only in `index.md`.
