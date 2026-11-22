# Phase 3: Current-task expansion and observability

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: Task 4

## Objective

Use the existing bounded tool loop for progressive context; never return repeated or stale source as fresh evidence. Measure selections and API input separately.

## Current-Code Evidence

| Path | Symbols / contract | Why it matters |
|---|---|---|
| `src/lean_weave/workflow/tools.py`, `src/lean_weave/workflow/tool_loop.py` | `WorkflowTools.bind_task`, `dispatch`, `ToolCallResult`, `run_tool_loop` | Fixed existing schema names and task scope; effect dispatch, cancellation and replay already centralized. |
| `src/lean_weave/context/packer.py`, `src/lean_weave/context/packing_session.py` | `open_task`, `expand`, `prepare_expansion_delivery`, `PackingSession` | Preview tracks references/coverage; preview receipts bind the exact replay, so cannot be reused with a changed envelope. |
| `src/lean_weave/executor/models.py`, `src/lean_weave/executor/patch.py` | `file_revision`, patch result `revision`, durability | Raw-byte patch precondition is not the canonical fragment digest. |
| `src/lean_weave/context_cache/service.py`, `src/lean_weave/metrics/jsonl.py`, `benchmarks/results.py` | Cache service, existing journal, `project_metrics` | Cache counters are local optimization; usage is journal-derived. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/workflow/{context_lifecycle,tools,tool_loop}.py` | Modify | Bounded read expansion, patch notification and current-round projection inside the same loop. |
| `src/lean_weave/context/{packer,packing_session,packing_source}.py` | Modify | Private scoped live rematerialization and coverage across owned revisions; leave preview receipt rules intact. |
| `src/lean_weave/context_cache/service.py`, `src/lean_weave/workflow/context_metrics.py` | Modify / create | Per-operation cache outcome observation and bounded workflow metadata event. |
| `tests/workflow/test_context_expansion.py`, `tests/workflow/test_context_metrics.py` | Create | Offline freshness/reuse and telemetry assertions. |

## Task 5: Bound expansion and rebuild after edits

### Intent
Give a live task additional source without widening approvals, repeating identical fragments or creating an independent tool loop.

### Implementation Steps
1. Only for an active V2 implement/retry attempt, wrap the existing `read_symbol` and `read_range` handlers after existing argument validation and task/approval checks. Preserve their schema names and request shapes; verify operation, task path and adapter read scope before opening anything. Retry expansion is restricted to the affected paths selected by Task 3. Reuse `ExpansionRequest` symbol/structural range resolution, `PackingCatalog` and session coverage by supplying the current registered canonical digest internally. Do not expose full-file/dependency selectors. For normalized ranges enforce existing tool range/character limits on request and resulting unit; promote Python to complete structure only if it still fits, and use bounded whole-line slices for admitted supported non-Python text. Unknown path, malformed selector, unsupported range and over-cap responses are typed metadata, never raw exception text.
2. Add a private scoped packing-session mode for workflow use. Initial pack seeds coverage with what was actually prepared for dispatch. During the existing bounded tool batch, resolve expansions against a fresh scoped witness and stage the newly covered refs; return matching tool replies with compact status, raw `revision` and path/line reference, with instructions that accepted source is in the current scoped source block. Do not put source text in tool replay. In the next prepared round, rematerialize current initial units and accepted expansion refs once, pin required units and successful deltas ahead of optional material, build the **actual** envelope including these tool replies, and recheck scope, freshness and all budgets before sealing. This private rematerialization path must reuse packer selection/fit and overlap logic; don't weaken `prepare_expansion_delivery` receipt/replay equality or forge a preview receipt. If the exact next envelope cannot fit, use the existing local refusal route, not a send that claims evidence was delivered. No extra assistant round on final round or on denied expansion; keep existing task-effect semantics.
3. Deduplicate by `(path, canonical digest, interval)` via session coverage. Exact or fully covered requests return `no_change` (plus current raw revision and existing ref) before source can be duplicated in the envelope; partial overlap only adds provably disjoint whole units or refuses the selector. Count recognized attempts, including duplicates, toward `max_expansions` for the current live attempt; enforce `cumulative_source_tokens` on unique units, including new revisions, with original per-file/total and per-request caps. An approved patch rebuild within the same attempt must not reset numeric caps. Task/feature token and call ceilings remain durable across verification-driven retries and resume; do not reconstruct a lost expansion conversation or grant additional retries. Finalization/cancel/interruption destroys refs, receipts and replay.
4. After an existing confirmed `apply_patch`, record its path and raw revision as in-memory rebuild authorization, leaving the checkpoint effect callbacks untouched. Before a same-batch read or next send, re-admit the scoped changed path and require the raw result revision to match; invalidate old path coverage/refs, retain unaffected current refs and cumulative counts, and assemble fresh source. If a same-batch patch supersedes a pending read reply, replace its not-yet-sent content with a metadata-only `stale_reference` result under the same tool ID. Do not rewrite already-sent replay; its revisions are historical, and only the current source block provides editable evidence. Changes to other recorded files or witnesses, stale/mismatched tool revisions, or uncertain patches never authorize rebuild; discard unproven payloads, return the existing typed stale/refusal or interruption, and never resend an old packed source block. A source change before the first send without an owned patch fails closed; clean retry/resume rebuild at normal stage entry from structured state. Preserve assistant tool-call arguments and reasoning needed by DeepSeek replay, but do not use old arguments as source authority.

### Required Interfaces and Contracts
- Existing tool schemas and approval/effect boundaries are unchanged. V2 read replies are internal model-facing protocol: `ok`/`no_change` reference current source and carry raw revision; refusal replies contain stable code only.
- Current-round authoritative source is one freshly assembled block; all stored refs/coverage are ephemeral. V1 tool results and preview L2 receipts stay compatible.
- Regenerate counted `expected_revision` entries with the source block after a verified owned edit. If the refreshed envelope cannot be safely sent, use Task 4's typed local refusal outcome; never return success or drop earlier usage/patch guards in a late-round early exit.

### Requirement Evidence
Original Request §§3, 5–8 and token efficiency; ADR-004; v1-05 raw expected revisions, v1-06 tool budgets/effects, v2-04 packing/expansion, v2-05 live proofs.

### Error Handling and Logging
DEBUG expanded selector kind, count, size and dedup count; WARN invalidated/stale/limit category without paths from denied inputs, tool arguments, diffs, source or replay.

### Tests
`test_context_expansion.py`: approved symbols/ranges, covered duplicate and partial overlap, multiple tools in one batch, read denied before source open, max attempts/cumulative caps even after a patch, unchanged revision vs external mutation, first CRLF patch gets `expected_revision` from the initial request with no read call, owned patch then same-batch read and next HTTP send use current revision, next-envelope no-fit preserves previous accounting with no new dispatch, unconfirmed patch no replay, cancel and clean resume. Retain preview session/receipt tests.

### Acceptance Criteria
Only validated scoped fragments reach the next request, same-revision duplicates are not reintroduced, and owned edits rebuild current evidence without resetting budgets.

### Verification
`uv run --locked pytest tests/workflow/test_context_expansion.py tests/workflow/test_tools.py tests/context/test_packing_session.py tests/context/test_packing_expansion.py` — all pass.

## Task 6: Record bounded context and stage telemetry

### Intent
Make token savings and retrieval work measurable without a new ledger or sensitive telemetry.

### Implementation Steps
1. In the private adapter record a stage/attempt-local context event on initial selection and each prepared round/refusal: mode/stage, outcome category, bounded per-attempt **opaque ordinal IDs** and kind for selected source/map fragments, counts, section input estimates, normalized wire input estimate, retrieval elapsed time (monotonic), expansion attempts/successes, and duplicate fragments/bytes avoided. Use a bounded map over internal fragment IDs; never emit raw IDs containing repository paths or content digests. Assign a new ordinal for a changed fragment; keep stable ordinals within the attempt. Emit DEBUG per round, INFO aggregate counts on stage completion, and WARN static refusal codes.
2. Instrument existing ContextCache operations with an optional in-process operation observer scoped to the builder. Count hits/misses by allowlisted cache kind and optional index fact hits; don't read before/after database-wide stats to infer per-stage activity. Cache misses/failure/disabling are not token savings. Do not persist these events in FeatureState or a new journal; allow injection of a test/benchmark collector or log metadata at the existing logging seam.
3. Link preparation to existing feature/stage/round accounting. Read provider input tokens and availability from existing journal attempt entries as the authoritative provider metric; use the gateway's journal `estimated_input_tokens` as the comparable normalized input estimate in **both** V1 and V2, not the V1 context estimate versus V2 packing counters. Retain V2 packing estimates and refused preflights as separate observations. Do not duplicate charging or invent zeros for absent usage. Distinguish confirmed sends and uncertain attempts; verification and diagnosis report zero model dispatches rather than simulated token usage.

### Required Interfaces and Contracts
- `workflow/context_metrics.py` owns bounded safe event fields and an optional observer taking only sanitized metadata. Observation failures do not authorize work or affect tool-loop outcome.
- Reuse `LEAN_WEAVE_LOG_LEVEL` for diagnostics; no request/response dump, full source, secret, approval token, path list, raw cache key, diagnostic body, or reasoning.

### Requirement Evidence
Original Request §10 and token efficiency; RULES logging/secrets; v1-08 accounting; v2-05 local versus API cost.

### Error Handling and Logging
For unavailable cache/usage expose unknown coverage separately from zero; retain event on local refusal and assert logging does not mutate budget, authority, or dispatch.

### Tests
`test_context_metrics.py`: safe fields/caps, no source/credential canaries in DEBUG, ordinal stability/dedup counts, enabled cold/warm and disabled observers, operation failure isolation, no double-count of provider counters, and no persisted event payload. Use focused metrics and benchmark regressions.

### Acceptance Criteria
Operators and offline benchmarks can explain selected fragments, estimates, retrieval time, expansions, cache reuse and stage input usage without inspecting prompts or changing accounting.

### Verification
`uv run --locked pytest tests/workflow/test_context_metrics.py tests/context_cache/ tests/metrics/ tests/benchmarks/` — all pass.

## Phase Risks and Mitigations
- Replacing assistant/tool pairs violates thinking replay; keep pairs and reasoning, with source only in the fresh base block.
- Expansion retries can multiply work; bound them within the existing loop, per-attempt expansion inventory and durable task/feature spend, with no independent retry driver.

## Phase Completion Checklist
- Tasks 5–6 meet acceptance criteria and both verification commands pass.
- Update only `index.md` for task progress after verification.
