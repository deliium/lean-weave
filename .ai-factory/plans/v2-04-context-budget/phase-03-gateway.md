# Phase 3: Explicit DeepSeek consumption

Plan: [index.md](index.md)  
Tasks: 6  
Depends on: Phase 2 / Tasks 4–5

## Objective

Let an authorized caller submit a ready pack to the existing DeepSeek gateway without changing or weakening the V1 workflow or gateway ledger.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/providers/deepseek.py` | `_prepare`, `_wire_request`, `_output_cap`, `complete`, `MODEL_CAPABILITIES` | Effective model/options, validated replay, final wire fields and pre-transport booking. |
| `src/lean_weave/contracts/_types.py`, `contracts/context_provider.py` | `LLMRequest`, `ContextProvider.build` | Extend only the optional request boundary; preserve provider and context protocols. |
| `src/lean_weave/metrics/budget.py`, `jsonl.py`, `models.py` | `estimate_request_tokens`, `charge_for_usage`, `ReportedUsage` | Composition limits are separate from existing feature admission and API settlement. |
| `src/lean_weave/workflow/tool_loop.py`, `budget.py` | Round assembly, replay and reservations | V1 orchestrator behavior stays untouched; no packing tool, replay pruning or automated follow-up. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/packer.py`, `packing_models.py`, `packing_tokens.py` | modify | Build immutable explicitly outbound-ready input and account for minimal verified carry-forward. |
| `src/lean_weave/context/packing_gateway.py` | create | Explicit adapter and envelope validation types, with no transport calls. |
| `src/lean_weave/contracts/_types.py`, `src/lean_weave/providers/deepseek.py` | modify | Optional packed-envelope guard at gateway boundary; reuse the pure Task 1 projection. |
| `tests/providers/test_deepseek_packed_context.py` | create | MockTransport acceptance and rejection; preserve existing gateway regression suites. |

## Task 6: Adapt ready packs to the existing DeepSeek request

### Intent
Send exactly the checked and budgeted content; use the existing gateway as the only network, retry and usage authority.

### Implementation Steps
1. Introduce `prepare_packed_request(...) -> LLMRequest` in `packing_gateway.py`. Require a ready pack, explicit `feature_id` and supported `stage`, and caller-authorized outbound path scope covering every source/map entry. This adapter does not grant read/write/tool/network authority; callers authorize preview and dispatch separately. Bind effective `model`, tool definitions/results, reasoning replay, response format, thinking, and reserved output cap to the immutable counted envelope. Use existing typed request fields, forward no pack metadata to the SDK, and attach an optional typed packed guard containing expected canonical wire fingerprint, resolved model, output cap, composition counters and limits. Do not encode source, tool contents, or query in the guard. The adapter does not call `complete`.
2. Require `ContextPacker.prepare_expansion_delivery(session, receipt, carry_forward_ids, *, envelope=None)` for **all** L2 dispatch, including a standalone delta with empty carry-forward. Reject foreign, missing, or closed receipts and duplicate/foreign/additional/missing IDs; the selected set must exactly match the private record's needed prior coverage. Check the freshly supplied envelope against the receipt's normalized-envelope fingerprint (the caller resupplies replay, if any). Reopen one strict scan and revalidate checkout, recorded revisions and originating edge; rematerialize the exact recorded prior and delta refs from it. Never trust bodies or IDs from a caller-supplied `PackResult`. Compose only the verified union, not an old map; recount the full envelope, character ceilings and output reservation. Delivery is read-only over coverage and cumulative new-source charge; stale revalidation still invalidates the session. Retrying an unchanged delivery cannot add inventory.
3. Use Task 1's shared pure projection for both fingerprint creation and gateway comparison, with the normalized effective wire fields and numerical output cap. The adapter checks readiness, outbound scope and counted limits before emitting a guarded request. A malformed guard (including explicitly present `None`) must not fall through to legacy behavior. The guard detects mismatches for trusted in-process use; it is not an authorization capability or a defense against a caller constructing its own raw `LLMRequest`.
4. Validate the opt-in guard after `_prepare` and `_wire_request`, before `_ensure_client` and reservation. Reject mismatched model/cap/projection and invalid numeric limits. Keep the existing V1 estimator, retry, journal, feature ledger and settlement; V2 composition admission is separate. Check `ReportedUsage.counters.core_complete()` and settlement charge on the journal's existing records in tests, not an assumed `LLMResponse.reported_usage` field. Do not log body/replay, credential-bearing content, or fingerprint.

### Required Interfaces and Contracts
- `LLMProvider.complete(request)` and `ContextProvider.build(stage, feature_state)` remain unchanged; the typed guard is an optional local request field, never on the wire. Feature/workflow stage wiring is untouched.
- Gateway accepts legal explicit V2 requests; it does not call retrieval, expand, or check the checkout itself. A caller needing newer source must ask the packing API to revalidate/rebuild before dispatch.
- No bare expansion delta may substitute for unavailable essential context. Tool messages are replayed through the normal validation path unmodified, not synthesized by the packer.

### Requirement Evidence
Original Request Architecture, 3–6, 12–19, DeepSeek acceptance; V1-03 gateway settlement; `.ai-factory/RULES.md` thinking replay and stage isolation.

### Error Handling and Logging
Sanitized validation for scope, guard/fingerprint mismatch and illegal envelopes; preserve existing provider errors for feature-budget and transport failures. DEBUG logs only count method, comparison totals and safe refusal codes.

### Tests
Use real `DeepSeekProvider` with MockTransport and isolated ledger. Verify envelope/body equality and max tokens, JSON mode, valid tool/reasoning replay and mismatched replay, resolved-model counter, tampered message/cap, insufficient and stale refusal before client/reservation/transport, independent feature-ledger denial, and complete vs partial API usage. Include wrong task/receipt, missing/extra IDs, replay supplied again unchanged vs changed, mutation after expansion but before delivery, empty carry-forward, and repeated delivery without cumulative recharge. Compare a minimal L2 follow-up and a replay-all comparator over identical required evidence; assert fewer locally counted aggregate input tokens on a fixture with unrelated prior code. Run existing DeepSeek and workflow compatibility tests.

### Acceptance Criteria
Packed input succeeds through the normal gateway with unchanged transport/ledger semantics; malformed packed input is never sent.

### Verification
`uv run --locked pytest tests/providers/test_deepseek_packed_context.py tests/providers/test_deepseek.py tests/providers/test_deepseek_crosslayer.py tests/workflow/test_budget.py` — MockTransport and old request paths pass.

## Phase Risks and Mitigations

- Normalization could invalidate a previously computed count: fingerprint the shared normalized payload and reject changes before any dispatch.
- A source inventory can be mistaken for prior server context: require explicit, recounted delivery for any needed source.

## Phase Completion Checklist

- Task 6 satisfies its acceptance criteria and verification passes.
- Update progress only in `index.md` after verified completion.
