# Phase 2: Context and workflow stats

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Phase 1 / Task 2

## Objective

Make workflow usage and local context observations available for active, interrupted and archived features without adding a telemetry store.

## Current-Code Evidence

| Path | Symbols | Why it matters |
| --- | --- | --- |
| `src/lean_weave/workflow/context_metrics.py`, `src/lean_weave/workflow/context_lifecycle.py` | `ContextStageEvent`, `_AttemptTelemetry`, `_record_pack`, `_finish` | Already emit bounded observations; the last pack is not cumulative API usage. |
| `src/lean_weave/context/models.py`, `src/lean_weave/context/packing_models.py` | `ContextPack`, `PackedContextV2`, `PackMetrics` | V1 and V2 expose distinct selection/estimator evidence. |
| `src/lean_weave/workflow/state.py`, `src/lean_weave/workflow/orchestrator.py` | `ControlRecord`, `_drive`, `_persist`, `_verify_stage`, `_run_tool_stage` | Existing atomic checkpoint and effect guards are the place to persist workflow observations. |
| `src/lean_weave/workflow/results.py`, `src/lean_weave/workflow/session.py`, `src/lean_weave/worktree/registry.py`, `src/lean_weave/worktree/manager.py` | `project_result`, `_read_only_view`, `list_routing_ids`, `_archive_state` | Stats need the same routing and state-directory identity as feature results. |

## Files to Change

| Path | Action | Required change |
| --- | --- | --- |
| `src/lean_weave/workflow/context_metrics.py`, `src/lean_weave/workflow/context_lifecycle.py` | Modify | Extend existing event projection where pack evidence is live. |
| `src/lean_weave/workflow/state.py`, `src/lean_weave/workflow/orchestrator.py`, `src/lean_weave/app.py` | Modify | Bounded context/stage observations in the existing control envelope; no new state store. |
| `src/lean_weave/workflow/session.py`, `src/lean_weave/metrics/analytics.py`, `src/lean_weave/worktree/manager.py` | Modify | Read/resume/archive projection without raw state exposure. |
| `src/lean_weave/cli/commands/stats.py`, `src/lean_weave/cli/app.py` | Create, modify | Thin Typer routing over the shared reporting projection. |
| `tests/test_workflow_telemetry.py`, `tests/test_cli_stats.py` | Create | Fixture-backed lifecycle and CLI contract tests. |

## Task 3: Checkpoint workflow and context observations

### Intent

Keep completed observations through resume, without mistaking absent telemetry for zero or bypassing existing interrupt rules.

### Implementation Steps

1. Add optional telemetry to `ControlRecord` with `next_stage_attempt`, one open attempt marker, and a bounded rolling tail of 128 closed stage attempts with truncation count. Each attempt holds ordinal, stage, nullable task ID, status, active duration, verification status/blocking flag if available, and bounded context summaries. Extend the existing stage/effect checkpoint rather than inserting a competing commit path: allocate ordinal once at stage entry and include it on Task 1 provider requests. Close the marker in the checkpoint that records the outcome. On resume, an old open marker is unobserved/interrupted unless the atomic outcome already exists; never close it as a success, re-use its ordinal or clear its authority guards. Legacy envelopes have no observations. Maintain cumulative committed active milliseconds across all completed stages (including those evicted from the tail); exclude pauses between invocations. Report partial duration when an open or lost stage has no complete timing.
   - Allocate at `_plan_stage`'s in-flight save, `_run_tool_stage`'s first existing guarded save before effects (carry the ordinal in memory through preparation/refusal), and `_verify_stage`'s `_begin_check_effect` save. Use null task for initial verification if no single task is active. Collect an internal event snapshot before `_run_tool_stage`'s success `_persist` (where `prepared.finish("done")` currently follows the save); keep external observer notification outside the atomic commit. Route bounded snapshots through `_terminal`/`_interrupt` for caught exits with a live in-process attempt; an open attempt loaded from disk has no clock or outcome and must not be settled from a later session's clock.
   - Enforce an optional serialized telemetry budget within `JsonCheckpointStore._encode`, using its actual byte limit. On overflow remove oldest detail, then drop optional observations/timing before ever failing a checkpoint that fit before telemetry. Preserve ordinal high-water through compaction where it fits; if even minimal telemetry cannot fit, mark joins unavailable rather than reusing an ordinal (reconcile to the journal high-water before allocating another, or omit ordinal attribution for that feature when it cannot be established). Never catch and retry state I/O/authority failures as telemetry errors.
2. Measure with monotonic clocks, including unsuccessful stage exits and context preparation errors on clean return. For V2 extend `_record_pack` and existing finish path with distinct source file/symbol counts, including typed symbols or fragment-to-definition metadata where available, pack input and section estimates and estimator label, capped per-stage maxima/last-pack values and successful expansion/attempt counts. Report dedupe fragment count and known bytes separately; do not factor observations into provider usage. Time retrieval/packing once around preparation and re-pack/expansion operations (replace overlapping inner timers or split them; no double counting). Report exact cache hits, exact misses and other/bypassed operations separately; disabled/no observer yields not applicable/unavailable, not guaranteed zero misses.
3. For V1, project `ContextBundle.pack` immediately after `build` using `ContextPack.excerpts` and selected `symbols`, estimate from its existing fields, and time build. Do not re-read files or infer V1 dedupe from V2. For an accepted fallback retain the V2 refusal and separate V1 snapshot under the same ordinal; journal dispatches identify `v1_fallback`. If the pack is not typed (injected caller), fields depending on it stay unknown. Map-only evidence is counted separately from source selections.
4. Add shared analytics projection joining stage attempts to journal rows on feature + ordinal; use the trusted `project_result` and control scope spends for task outcomes, workflow retries, verification and budget caps/consumption. Workflow failure/retry-terminal slice is distinct from failed sends; a later verification failure associates with workflow repair/failure metrics, not a rewrite of a successful provider response. Use nullable outcome for dropped/legacy ordinals. Do not infer ranking quality in normal workflows; it requires Phase 3 fixture labels.

### Required Interfaces and Contracts

- Persist bounded counts, typed statuses/labels, task IDs and ordinals only; no prompt text, paths, provider raw usage, cache identifiers or full events. Normal runs still have no second spend source. Leave `wl feature result` schema and workflow transitions unchanged.
- `reporting_snapshot` and checkpoint are independently atomic; in-flight stats mark possible partial coverage rather than claiming a cross-file transaction. Any optional observer failure remains non-authorizing and non-blocking.

### Requirement Evidence

Original Request §§1–4 and acceptance 1, 5, 6; verify V1, V2, fallback and interrupted/archived lifecycle cases.

### Error Handling and Logging

DEBUG stage, ordinal, outcome and counts; WARN once per stats request for unavailable telemetry. Reject corrupt checkpoint data with existing domain errors; never include diagnostics/text in the report.

### Tests

Use actual orchestrator transitions with existing V1 workflow fixtures for verify→repair; assert planning versus task attribution, resume with unresolved reservation, interruption after stage outcome, stage replay not duplicating observations, fallback, refused/no-send paths, retention and timing, disabled versus hit/miss, observer failure, and legacy control deserialize.

### Acceptance Criteria

Two-stage resume and verify→repair retain known usage and do not double count; archived results retain the same projection.

### Verification

`uv run pytest tests/test_workflow_telemetry.py tests/test_metrics_analytics.py` — pass offline.

## Task 4: Expose read-only stats commands

### Intent

Give operators usable feature and checkout-wide breakdowns without starting workflows.

### Implementation Steps

1. Add a read-only `FeatureSession` stats resolver using the route, recorded `state_dir`, validated live/archive roots and `JsonCheckpointStore` identity from `_read_only_view`; do not reuse that view's catch-all exception path, reconstruct authority from user paths, or use display paths. For a checkpoint/journal that disappears or changes identity during collection, re-read and revalidate the route once; if it moved, discard the first snapshot and read both from the new location. If still transitioning, report busy/partial. A malformed checkpoint or journal under an unchanged route is a typed integrity error. Avoid diff, provisioning, verification, provider setup and state mutation. For checkout stats enumerate `list_routing_ids` in sorted order, with no cross-repository scan; legacy/probe journals without a routed feature are not implicitly loaded.
2. Correct `WorktreeManager._app_only_paths` to recognize only the default `metrics/llm-usage.jsonl` and corresponding lock within the recorded state directory so existing clean/terminal archival can carry the real journal; preserve nested, symlink, regular-file, dirty-worktree and cleanup refusal checks. Do not accept arbitrary paths under `metrics/`.
3. Add `wl stats` (default callback, `invoke_without_command`) and `wl stats feature <id>` with `--json` on each. No routes returns an empty report, unknown feature returns not-found, routing-only returns an otherwise successful unavailable/partial report. A selected corrupt feature fails with the shared CLI error path; in inventory mode preserve valid rows and report per-feature integrity failures with partial coverage and nonzero exit. Report feature, stage, task, model and strategy breakdowns, verification outcome, both retry counts, and labeled token/clock sources. Never use feature status as proof of provider usage coverage.

### Required Interfaces and Contracts

- Versioned JSON view and readable default table; deterministic ordering; omit raw metadata/paths. CLI stdout contains only the requested output; diagnostics go to stderr. No writes, including lock-file creation, on queries.

### Requirement Evidence

Original Request §§3, 12–13; route active, archived, empty, corrupt and unknown states distinctly.

### Error Handling and Logging

DEBUG matched/partial route counts; use sanitized domain failures, no root or raw journal data in error output.

### Tests

`tests/test_cli_stats.py` uses Typer `CliRunner` and real temporary production registry/state paths for no routes, live then archive parity, deterministic route change during a read, invalid feature ID, unchanged-route corruption and partial coverage; spy that no network, check or patch is invoked and filesystem is unchanged. In `tests/test_workflow_telemetry.py` add near-limit checkpoint encoding and an interruption between the outcome save and external observer notification to prove committed observations do not depend on the observer.

### Acceptance Criteria

Both commands work before and after feature cleanup; totals match the Phase 1 projection.

### Verification

`uv run pytest tests/test_cli_stats.py` — all routing and output tests pass.

## Phase Risks and Mitigations

- A crash can precede a context outcome checkpoint: show interrupted coverage; the independently durable journal still preserves booking/usage.
- Do not broaden the V2 fallback, source-admission or command-execution policy to make telemetry available.

## Phase Completion Checklist

- Both tasks meet acceptance; offline verification passes.
- Mark Tasks 3–4 in `index.md` after verification.
