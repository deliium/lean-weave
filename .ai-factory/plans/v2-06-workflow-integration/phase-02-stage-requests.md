# Phase 2: Stage projections and guarded sends

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Tasks 1–2

## Objective

Use scoped V2 context for planning, implementation and retry through the existing driver, with deterministic verification/diagnosis and exact per-request budget admission.

## Current-Code Evidence

| Path | Symbols / contract | Why it matters |
|---|---|---|
| `src/lean_weave/workflow/orchestrator.py`, `src/lean_weave/workflow/stages.py`, `src/lean_weave/schemas/models.py` | `_plan_stage`, `_implement_stage`, `_verify_stage`, `_retry_stage`, `_run_tool_stage`, `ready_task`, `TRANSITIONS` | Hooks are after existing policy gates; stage model and review/cancel semantics stay unchanged. |
| `src/lean_weave/workflow/planning.py`, `src/lean_weave/workflow/instructions.py` | `build_plan_request`, `parse_plan_response`, `load_instructions` | Plan output contract and registered trusted fragments must be counted before sealing. |
| `src/lean_weave/verify/runner.py` | `ApprovedChecksRunner.run`, `RepairAttribution`, `repair_attribution` | Strict attribution already rejects ambiguity/infrastructure errors and stores structured verification. |
| `src/lean_weave/workflow/{tool_loop,budget}.py`, `src/lean_weave/context/packing_gateway.py`, `src/lean_weave/providers/deepseek.py` | `run_tool_loop`, `reserve`/`settle`, `prepare_packed_request`, `DeepSeekProvider.complete` | Existing loop rebuilds requests; gateway guard catches mutations before journal reservation/transport. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/workflow/{context_lifecycle,orchestrator,planning}.py`, `src/lean_weave/verify/runner.py`, `src/lean_weave/context/builder.py` | Modify | Stage projections, compact safe diagnostics, hooks and same-scope V1 rendering for fallback. |
| `src/lean_weave/context/packer.py`, `src/lean_weave/context/packing_models.py` | Modify | Internal scoped required regions, metadata-only planning assembly and effective output cap. |
| `src/lean_weave/context/packing_tokens.py` | Modify | Counted workflow-only raw revision entries for editable paths; leave preview rendering unchanged. |
| `src/lean_weave/workflow/{tool_loop,budget}.py`, `src/lean_weave/context/packing_gateway.py` | Modify | Optional prepared request callback and normalized estimate; reuse existing wire guard. |
| `tests/workflow/test_context_lifecycle.py` | Create | Projection, mode and fail-closed coverage. |
| `tests/workflow/{test_planning,test_budget,test_orchestrator}.py`, `tests/providers/test_deepseek_packed_context.py` | Modify | Response parity, wire/replay, admission and transition tests. |

## Task 3: Project context for each existing stage

### Intent
Avoid replaying the feature plan, prior task context, or broader diagnostics when a small projection is sufficient.

### Implementation Steps
1. In `_plan_stage`, choose V2 locally where legacy uses `build("plan", state)`. Reuse the V1 metadata projection for bounded project requirements/architecture rules, but build a single trusted system block from deduplicated registered fragments and a single task block with the exact user request, bounded metadata, stable approved paths and the existing JSON output contract. Extract reusable plan-contract text from `build_plan_request` while retaining V1 output exactly. Compose L0 map rows only, under independent map limits; an empty map is valid in workflow, unlike unavailable preview L0. If the required request or output contract cannot fit, refuse rather than truncate it. Preserve `parse_plan_response`, the original policy validation, review publication and typed library bypass (`run_simple` without a PLAN request).
2. In `_run_tool_stage`, after review, approval and check preflight, bind tools and construct a task specification from the active task title/description, approved task files, relevant constraints and direct prerequisite IDs/status only if needed to distinguish completed prerequisites. Do not serialize the plan, other task descriptions, completed-task prose or previous-stage messages. For implement, reserve one mandatory complete source unit per active target before optional ranked definitions/dependencies/tests. Choose from positive ranked structural units first, then the first admitted definition or first complete statement (e.g. assignment-only Python); for supported non-Python text use a bounded complete line unit when no symbol exists. A missing required target or non-fitting unit is explicit, not a reason to omit that target and send anyway. Keep source and repository text labeled as data; only registered/trusted instructions belong in the system role.
3. Keep `_verify_stage` calling the existing local verifier with approved checks and check-effect markers; no `ContextProvider.build`, V2 scan, or LLM there. Diagnosis is the existing deterministic attribution step, not a new phase of the driver. Reuse `repair_attribution` for the retry projection (recompute from saved `VerificationResult` on resume); keep only bounded unique recognized code/path/positive line, with safely validated approved check IDs joined from matching original blockers, never raw logs/summaries. Require the same single attributable task; don't widen write or tool scopes. Select current complete enclosing definition or statement at each retained diagnostic on attributed paths; if a valid line has no enclosing structure, use the bounded admitted line through existing structural range rules, otherwise refuse unresolved mandatory evidence. Use `diagnose` ranking profile only for those paths. Current snapshots reflect worktree edits; do not request raw diffs. Keep only the latest existing structured failure for retry, with no extra failed-attempt history.
4. V2 context refusal before the first send: map a proven local packing cap refusal to `budget_exhausted` and source/selector insufficiency to `failed/context_insufficient`; unknown or stale proof to `failed/context_stale` without new effects. In `v2-with-v1-fallback`, allow the existing V1 builder/rendering for **implement only** when the scoped proof is complete and the sole reason is `no_relevant_source` (e.g. no supported complete anchor on safe text), before the first send or effect of that stage attempt. Supply the builder the same frozen scope and admitted sources; cap the finalized fallback envelope to the same V2 whole-input/output and stage ceilings. For fallback tool reads, retain the same approval/scope, current revision checks and cumulative V2 attempt/source caps; recount each normalized fallback round before sending, never downgrade to an uncapped V1 loop. Do not use fallback after any budget, authority, incomplete admission, stale witness, wire guard or provider refusal. Selection is sticky within that stage attempt; no dual build on successful V2. Keep optional index/cache misses as normal V2 degradation, not V1 fallback.
5. In the V2 user source section render one ordered `(path, expected_revision)` entry per editable path backed by current admitted source in the request. Use the executor-compatible raw revision from that same snapshot, not the canonical digest in `render_fragment_header`. Count these entries in the source section and complete envelope before sealing. Instruct the model to use `expected_revision` for `apply_patch` and not read again merely to obtain it. Do not advertise an edit revision for optional dependencies or a retry path outside the attributed scope. Use the same counted entries for admitted editable evidence in the scoped V1 rendering fallback.

### Required Interfaces and Contracts
- A private workflow stage input carries only read/output scopes, bounded spec/instructions, stage and deterministic target selectors. The adapter owns context choices, not state transitions.
- No new `Stage`, durable data fields or serialized messages. Existing unsafe/ambiguous attribution continues to fail before a retry request.

### Requirement Evidence
Original Request §§2–6 and 8–9; ADR-002/004; v1-06 transition/bypass rules; reconciliation plan/implement/verify/retry/fallback rows.

### Error Handling and Logging
DEBUG stage/selector/omission counts and fallback choice, WARN bounded refusal categories. Do not log diagnostic text, raw paths from refused input, request contents or state dumps.

### Tests
`test_context_lifecycle.py`: source-free plan with map and approved paths, empty map, invalid plan unchanged, active-task isolation, multiple targets and assignment-only Python, scoped fallback/no fallback reasons, retry current source and sanitized attribution, and exactly one counted edit revision per writable path with none for dependency-only evidence. `test_orchestrator.py`: eligible/ambiguous failure, retry cap, review and bypass, no LLM on verify/diagnosis or after success.

### Acceptance Criteria
V2 delivers only the selected stage's evidence without changing transition, approval or result behavior; unsafe source and over-cap required material never dispatch.

### Verification
`uv run --locked pytest tests/workflow/test_context_lifecycle.py tests/workflow/test_planning.py tests/workflow/test_orchestrator.py tests/workflow/test_feature_e2e.py` — all pass.

## Task 4: Seal and charge the exact wire request

### Intent
Use existing providers and accounting without adding hidden retries, stale guards, or different effective output reservations.

### Implementation Steps
1. Add an optional internal prepared-round callback to `run_tool_loop`. It receives the current stage-local assistant/tool suffix only, normalized options known to the workflow, and bound schemas. V1 still uses its existing request constructor. The V2 callback assembles the exact two base messages and suffix with `PackEnvelope`, real model/thinking, tool schemas and response format; only then call `prepare_packed_request`. Keep `options.retry_limit=0` as a non-wire option; never change counted fields after sealing.
2. Before composition, pick `min(effective LLM cap, packing output cap)` and use it at every V2 layer, including `max_output_tokens`. Keep existing provider normalization, enforce independent packing sections, whole request plus output, and existing stage context and workflow feature/task/call ceilings. Plumb a normalized-wire input estimate into existing workflow reservation using the same ledger estimator/projected wire inputs as the gateway (including thinking `extra_body`, tools and structured format). This is not `pack.metrics.input_tokens`; keep separate pack counters. For matched measurement compare equivalent effective caps, not preview defaults. Do not replace gateway-side validation or journaling, and do not create a second usage ledger.
3. Keep existing workflow reservation/settlement, cancellation and pending markers. Build and validate initial V2 context before marking an in-flight *dispatch*; retain the existing persisted in-flight stage and effect guards immediately before the first provider operation, including after preparation. Check cancellation before local work and again just before booking/dispatch. Use existing stage failure/interrupt routes for local refusal; if a response or edit effect is unresolved, preserve its marker and never retry through fallback. If a patch is confirmed and a later local refusal prevents completion, keep the edits and finish through the existing guarded outcome path; do not report task success or treat the edit as replay-safe if checkpointing fails.
4. Every round receives a new guard over what will actually be sent. Keep only current-loop assistant/tool pairs with matching IDs and required DeepSeek reasoning for thinking-enabled replay. Do not persist them or share with another stage. Stop before tools on reported overage; keep current final-assistant task completion semantics (a successful patch alone is not a verified outcome). Do not add synthetic model turns for context or diagnosis.
5. Represent known local preparation/budget refusal as a typed `ToolLoopOutcome` field with a stable category and reason, preserving accumulated `calls`, `usage` and events. Return it instead of letting a preparation exception escape across `_accumulate_metrics`. Both `_plan_stage` and `_run_tool_stage` accumulate once before projecting failed/budget results. Initial refusal books no call; refusal after a settled response retains spend without booking another. If a patch has a pending or uncertain guard, interrupt with the marker intact before handling a context refusal. For a durably confirmed edit followed by refusal, retain the edit and atomically persist the non-success outcome and clear only the settled guard; on commit failure the previous checkpoint still requires inspection. Leave existing gateway exception/uncertain accounting and V1 behavior unchanged. A context refusal never makes a task `DONE`.

### Required Interfaces and Contracts
- Existing `LLMProvider.complete` remains unchanged; normal `LLMRequest` with optional guard. Test the normalized wire at the same seam used by live transport.
- The V2 callback is invoked inside the existing bounded loop; no retry loop or execution authority is added.

### Requirement Evidence
Original Request §§3, 7–8, 10 and token efficiency; ADR-004; v1-03 gateway accounting, v1-06 reservations, v2-04 `prepare_packed_request`.

### Error Handling and Logging
DEBUG exact-cap/round/call and distinct estimate dimensions; WARN static refusal/overage codes only. Proven local preflight rejection makes no send, uncertain sends remain conservatively charged, and guard tampering never causes V1 fallback.

### Tests
Extend `test_budget.py` and `test_deepseek_packed_context.py`: think on/off exact HTTP body, plan JSON mode, first and subsequent guards, altered request rejected before gateway reservation, V1 wire goldens, pre-dispatch limit, reported overage before tools, missing usage and resumed spend. In `test_orchestrator.py`, cover local refusal before the first send, after a settled response, after a confirmed edit and its failed terminal checkpoint, and with an uncertain patch marker. Assert exact prior call/spend retention and no additional dispatch. Test cancellation before local preparation and between preparation and dispatch.

### Acceptance Criteria
Every packed send is guarded and independently budgeted; retry scope shares task spend, and a refused request cannot trigger an additional route to the provider.

### Verification
`uv run --locked pytest tests/workflow/test_budget.py tests/workflow/test_orchestrator.py tests/providers/test_deepseek_packed_context.py tests/providers/test_deepseek_crosslayer.py` — all pass.

## Phase Risks and Mitigations
- Adding output instructions after guarding breaks integrity; count the exact finalized wire instead.
- Verification logs can contain arbitrary repository output; only allowlisted structured diagnostics enter a retry projection.

## Phase Completion Checklist
- Tasks 3–4 meet acceptance criteria and both verification commands pass.
- Update only `index.md` for task progress after verification.
