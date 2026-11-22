# Phase 4: Offline acceptance and rollout

Plan: [index.md](index.md)  
Tasks: 7–8  
Depends on: Task 6

## Objective

Prove equivalent workflow correctness and safety while measuring context cost on matched V1/V2 feature runs. Leave V2 opt-in.

## Current-Code Evidence

| Path | Symbols / contract | Why it matters |
|---|---|---|
| `benchmarks/fixtures/`, `benchmarks/offline.py` | Pinned manifest/source pairs and in-process CLI over `httpx2.MockTransport` | Exercises real Git/worktree/session/gateway without contacting DeepSeek. |
| `benchmarks/results.py`, `docs/benchmarks.md` | Read-only journal/checkpoint projection and V2 comparison protocol | Keeps local estimates, synthetic usage and real provider usage separate. |
| `tests/cli/test_feature_offline_e2e.py`, `tests/cli/test_feature_recovery_e2e.py`, `tests/cli/test_feature_isolation_e2e.py` | Real CLI, review, recovery and isolation fixtures | Extend these rather than simulating feature orchestration. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `benchmarks/offline.py`, `benchmarks/results.py` | Modify | Select workflow context mode in test operator config and label paired evidence without changing existing persisted report readers. |
| `tests/cli/test_feature_context_e2e.py`, `tests/benchmarks/test_workflow_context.py` | Create | Public offline feature parity and matched token/quality assertions. |
| `docs/architecture.md`, `docs/worktree-workflow.md`, `docs/context-packing.md`, `docs/benchmarks.md` | Modify | Settings, lifecycle, failure and comparison instructions. |

## Task 7: Prove paired workflow acceptance

### Intent
Show the actual feature entry point uses V2 without changing worktree, policy, lifecycle or provider accounting semantics.

### Implementation Steps
1. Add a default-preserving workflow-context selector to the offline fixture driver. Run each arm in a fresh materialized Git repo and external trusted operator config with identical pinned manifest, check definitions, permissions, request, model, thinking, limits and scripted decisions. Use the real gateway journal. Add context-mode labels to the new comparison projection, retaining legacy baseline decoding and making missing usage explicit.
   - For the representative paired success run, let the scripted model obtain `expected_revision` from the captured request when available; otherwise issue the fixture's already-approved `read_range` and use its returned revision. Keep the plan and desired edits identical. The patch builder may use the pinned edit recipe, but must not substitute `ContentShadow.revision` or read the worktree to supply a missing revision. Assert the first V2 patch uses the initial request with no read just for a revision; count any legitimate extra V1 read round as observed work.
2. Run `bug_fix`, `multi_file_feature`, `verify_then_repair`, `insufficient_context`, and `budget_exhaustion` through both modes. Use `benchmarks/fixtures/manifests/multi_file_feature.json` with the pinned sources and approved oracle. Give the repair case its explicit fail→repair script (the historical baseline's zero-retry result is not repair evidence); assert two check batches and one attributed retry. The existing insufficient-context case tests plan rejection; add a separate valid-plan/non-ready V2 pack scenario with no implementation send. Assert oracles, calls and checks, active-task context and exclusion of unrelated snippets, unchanged origin HEAD/index/files, no verification/diagnosis LLM sends, and no post-success model call. Include the typed `run_simple` library bypass separately, not a new public CLI flag.
3. With the same real CLI composition, exercise review pause with no write/check, stale approval after an approved-file edit, clean approval with no planning replay, execution interrupted at an existing safe checkpoint then resumed via a fresh session, unresolved effect producing inspection without replay, and cooperative cancellation without a subsequent effect. Retain terminal read-only/archival parity. Use deterministic injected hooks or synchronization, never timing sleeps.
4. Compare cumulative `estimated_input_tokens` from the real gateway journal for matched correct runs, summing the same estimator over actual attempts in each mode with per-stage and send-count breakdown. Keep packing estimates, refused preflights, uncertain attempts and scripted provider usage separate. Require a strict reduction on the pinned `multi_file_feature` fixture; do not manufacture a saving by padding V1, altering task/check policy, omitting required evidence, or comparing failed runs against successful runs. Keep cold/warm and index availability labeled; report all other case deltas without a universal improvement or live usage claim.

### Required Interfaces and Contracts
- Tests use the same offline gateway/ledger and data-only fixture oracles; no live API, forced cleanup, new public command, or runtime dependency.
- A separate workflow-context comparison projection must not reinterpret V1 context-size fields or change accounting. Failed and uncertain attempts remain visible.

### Requirement Evidence
Original Request Testing and Acceptance 1–8; v1-08 paired baseline; reconciliation rows on auto/bypass/review, retries/resume, fallback and accounting.

### Error Handling and Logging
Assert metadata-only DEBUG diagnostics and stable warning codes. On comparison mismatch identify case, mode and invariant, not serialized requests, source, approval tokens, or raw check output.

### Tests
`tests/cli/test_feature_context_e2e.py`: paired manifest cases plus review, safe/uncertain resume and cancel. Assert a scripted patch fails its fixture contract if the envelope/read result does not expose a valid revision. `tests/benchmarks/test_workflow_context.py`: matching settings, common journal-estimator stage rollups, missing estimates and partial usage, uncertain attempts, and correct denominators; reuse Task 6 redaction sentinels.

### Acceptance Criteria
Pinned fixtures pass the same correctness oracles, negative outcomes remain equivalent, review/resume are safe, and a representative matched correct run shows reduced estimated input.

### Verification
`uv run --locked pytest tests/cli/test_feature_context_e2e.py tests/cli/test_feature_offline_e2e.py tests/cli/test_feature_recovery_e2e.py tests/cli/test_feature_isolation_e2e.py tests/benchmarks/` — offline success.

## Task 8: Document opt-in and finish regression gates

### Intent
Expose only verified operator behavior and make rollback clear.

### Implementation Steps
1. At the mandatory docs checkpoint, use `/aif-docs` to document the chosen setting and trusted config example, planning/bypass/review and resume behavior, deterministic diagnosis, stage budgets, expansion freshness and session lifetime, optional dependency degradation versus V1 fallback, and terminal behavior.
2. Explain that rollback is choosing V1 for new features; do not advise swapping a frozen configuration to resume an active V2 feature. Explain checkout-local caches, no implicit peer reuse or maintenance, and no new effect authority.
3. Document exact offline paired commands, telemetry units/coverage, cache/time conditions, and the difference between local cache reuse, local input estimates, scripted provider tokens, and measured live API usage. Keep live validation optional and separate.
4. Run the locked full suite, including existing dependency, CLI and safety tests. Verify docs examples against CLI help and targeted fixtures; do not mark the roadmap, rewrite accepted ADRs, or introduce runtime LangGraph dependencies.

### Required Interfaces and Contracts
New documentation describes product config, not `.ai-factory/config.yaml`. CLI commands, exit codes and checkpoint contracts remain unchanged.

### Requirement Evidence
Original Request §§8–10 and Acceptance criteria; ADR-002/004; `docs/benchmarks.md` comparison protocol and `docs/worktree-workflow.md` trusted config contract.

### Error Handling and Logging
No runtime logging changes; examples show only safe reason codes, counts and estimates. Never paste traces containing prompt or credential material.

### Tests
Existing locked full suite plus the Task 7 paired comparison; verify `uv run --locked wl --help` and examples offline.

### Acceptance Criteria
Docs are runnable, rollback limitations are explicit, and full regression tests pass without new dependencies.

### Verification
`uv run --locked pytest` and `uv run --locked wl --help` — offline success and unchanged entry point.

## Phase Risks and Mitigations
- Synthetic provider counters can look like live savings; label their provenance and use comparable preflight estimates for offline efficiency evidence.
- Fixture success can hide worktree escapes; compare origin fingerprints, real target paths and terminal results in the existing Git fixtures.

## Phase Completion Checklist
- Tasks 7–8 meet acceptance criteria and both verification commands pass.
- Update only `index.md` for task progress after verification.
