<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Token-Aware Context Packing and Progressive Expansion

Plan ID: `v2-04-context-budget`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-22  
Depends on: [v2-01-code-indexer](../v2-01-code-indexer/index.md), [v2-02-repository-map](../v2-02-repository-map/index.md), [v2-03-context-ranking](../v2-03-context-ranking/index.md), and [v1-03-deepseek-gateway](../v1-03-deepseek-gateway.md) (all tasks checked).

## Original Request

Plan ID: v2-04-context-budget
Plan Title: Token-Aware Context Packing and Progressive Expansion
Milestone: V2 - Context Intelligence

Dependencies:
- v2-01-code-indexer
- v2-02-repository-map
- v2-03-context-ranking
- v1-03-deepseek-gateway

Goal:
Implement a deterministic context packing system that selects relevant source fragments within configurable token budgets.

Architecture:
- Preserve ContextProvider compatibility.
- Integrate with the existing DeepSeek Gateway.
- Keep context construction independent of the orchestrator.
- Do not introduce LangGraph.

Progressive context levels:

L0: Compact repository map
L1: Relevant symbols and source definitions
L2: Expanded source ranges or full files when justified

Requirements:

1. Implement ContextPacker.
2. Use ranked retrieval candidates.
3. Support configurable budgets for:
   - system instructions
   - task specification
   - repository map
   - source fragments
   - tool definitions and results
   - output token reservation
4. Use an accurate tokenizer when compatible tooling exists.
5. Otherwise use conservative estimates.
6. Distinguish estimated tokens from API-reported usage.
7. Select source fragments by relevance and token cost.
8. Deduplicate overlapping source ranges.
9. Avoid repeated signatures and definitions.
10. Preserve code structure and source integrity.
11. Never silently truncate essential code.
12. Support on-demand context expansion.
13. Allow additional retrieval by:
    - symbol
    - source range
    - related dependency
14. Limit expansion count and cumulative context size.
15. Track fragments already supplied within a task.
16. Detect source revision changes.
17. Reject stale context references.
18. Return explicit insufficient-context results.
19. Expose context composition metrics.
20. Preserve the existing ContextPack interface or introduce a compatible versioned extension.

ContextPack metadata:
- task identifier
- selected fragments
- source references
- selection reasons
- source revisions
- estimated token usage
- remaining budget
- included fragment identifiers

CLI:
- wl context build <query>
- wl context inspect <query>
- wl context budget <query>

Token-efficiency requirements:
- Minimize total processed tokens, not only initial prompt size.
- Do not repeatedly send unchanged unrelated code.
- Avoid full-file context unless necessary.
- Reserve space for responses and tool schemas.
- Keep context expansion bounded.
- Never sacrifice essential correctness information merely to meet a target prompt size.

Testing:
- Budget enforcement
- Overlapping source ranges
- Large functions
- Missing dependencies
- Stale source revisions
- Context expansion
- Token estimation errors
- Unicode source

Acceptance criteria:
- Context packs respect configured budgets.
- Relevant source provenance is preserved.
- Context expansion is controlled.
- No essential source is silently corrupted or truncated.
- DeepSeek Gateway can consume the resulting context.
- Existing V1 interfaces remain compatible.

Out of scope:
- LLM-generated context summaries
- Embeddings
- Vector databases
- Autonomous repository exploration

Save using the exact plan ID:
v2-04-context-budget

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes (mandatory `/aif-docs` checkpoint)

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: This follow-up composes the completed index, map and ranking capabilities into budgeted prompts; it does not reopen or edit the completed roadmap milestone.

## Requirements Reconciliation

Authority: the Original Request defines V2 scope; `.ai-factory/RULES.md` and `.ai-factory/ARCHITECTURE.md` constrain stage isolation, admission and deterministic operation. Existing interfaces and completed dependency plans define compatibility, not new feature behavior. Existing V1 preview can omit or fall back to signatures; the new V2 integrity requirement applies to packed results without changing V1.

| Decision / supported combination | Source anchor | Verification evidence |
|---|---|---|
| **Initial L0 / L1:** metadata-only map at L0; L1 admits essential checked definitions before optional evidence. If required evidence cannot fit or be resolved, return typed insufficient without prompt; no session for non-ready initial results. | Original Request levels, 1–2, 7–11, 18; V2-02 map and V2-03 referenced windows | Tasks 2–3: large decorated function, competing cheap hit, zero map budget, overlaps, stable provenance. |
| **Indexed / degraded:** same-scan checked graph when available, live metadata/definitions when unavailable; apply output path scope before the packing shortlist, not before whole-checkout admission. Incomplete scans are marked; changed witnesses invalidate the result. No cached bodies or index refresh. | Original Request 2, 10, 16–17; V2-01 admission; V2-02 generation; V2-03 retrieval | Tasks 2–3: scoped top-K, true match anchors, multiline decorators, Unicode and witness change. |
| **Token counting:** count normalized input from Phase 1 onward; exact local adapter only when mapped to the effective model and wire shape, otherwise conservative byte-based estimate. Independent section and total limits, output held back; neither estimate substitutes for feature spend or API usage. | Original Request 3–6, 19 and token efficiency; V1-03 ledger | Tasks 1, 6: exact/over-cap, Unicode, adapter failures, normalized replay, partial/complete journal usage. |
| **L2:** explicit symbol, structural range/full file, or one registered fully resolved edge. Invalid caller references do not consume attempts or close the session; recognized attempts are bounded; successful deltas alone add coverage/cumulative units. Missing graph and actual changed revisions are distinct outcomes. | Original Request 12–18; V2-02 relations; V2-03 dependency provenance | Tasks 4–5: unresolved-member edge, wrong digest vs observed revision drift, count and size limits, overlap and full-file justification. |
| **Dispatch:** authorized caller only; an L2 delivery receipt binds the originating delta, fresh replay and exact carry-forward refs. Revalidate and recount that request without charging prior source to new inventory; budget refusal has no transport side effect. No V1 stage, checkpoint or tool change. | Original Request Architecture, 14, token efficiency and acceptance; `.ai-factory/ARCHITECTURE.md` workflow; V1-03 | Task 6: MockTransport, receipt and mutation rejection, unchanged repeat delivery, independent ledger refusal, aggregate token comparison. |
| **CLI:** legacy default `build TASK` / `inspect PATH` preserved; explicit flags win over the config default, conflicting explicit modes fail, and `budget QUERY` is V2-only. No path-existence heuristic or config migration. | Original Request CLI and V1 acceptance; `src/lean_weave/cli/commands/context.py` build/inspect | Tasks 7–8: dispatch matrix, JSON parity, a controlled copy of existing tracked `src/lean_weave/context/python_index.py`, legacy regressions. |

## Architecture and Decisions

- `ContextPacker` is a read-only opt-in composition surface: request → existing V2 retrieval inside a strict scan → materialized whole units and map → final witness → versioned pack. The optional output allowlist filters emitted data, not whole-checkout ranking reads; do not place this preview behind a V1 stage's read approval.
- L0 and L1 are initial levels; L2 is an explicit in-process follow-up. Session state tracks revisions/coverage and delivery receipts, not source or transcripts. Cumulative source inventory measures unique additions, not provider spend. An L2 delivery revalidates only the registered task evidence and counts the explicit required carry-forward and resupplied replay.
- `PackedContextV2` does not change `ContextPack` or `ContextProvider`. The caller can opt into the existing DeepSeek gateway using a budgeted, normalized envelope; the gateway does not fetch or expand context. No autonomous loop, LangGraph, embeddings, vector storage, or LLM summaries.
- Product YAML defaults retain legacy behavior. `context.packing.cli_default: true` enables bare `build QUERY` and `inspect QUERY`; otherwise use `--packed`. `budget QUERY` works in either mode. No live API access is needed for preview or acceptance.

## Phase Index

1. [Phase 1: Deterministic initial packing](phase-01-packing.md) — Tasks 1–3
2. [Phase 2: Bounded task-local expansion](phase-02-expansion.md) — Tasks 4–5
3. [Phase 3: Explicit DeepSeek consumption](phase-03-gateway.md) — Task 6
4. [Phase 4: Offline CLI and acceptance](phase-04-cli-acceptance.md) — Tasks 7–8

## Cross-Phase Dependencies

- Task 4 depends on Task 3 for initial ready packs and checked revision/coverage input.
- Task 6 depends on Tasks 4 and 5 for verified delta/carry-forward semantics.
- Task 7 depends on Task 3's normalized offline composition contract, not on the later gateway guard.
- Task 8 depends on Tasks 6 and 7 for end-to-end compatibility and documentation.

## Tasks

### Phase 1: Deterministic initial packing

- [x] Task 1: Define pack contracts and token accounting ([details](phase-01-packing.md#task-1-define-pack-contracts-and-token-accounting))
- [x] Task 2: Bind ranked candidates to checked source ([details](phase-01-packing.md#task-2-bind-ranked-candidates-to-checked-source)) (depends on 1)
- [x] Task 3: Select and render initial context ([details](phase-01-packing.md#task-3-select-and-render-initial-context)) (depends on 1, 2)

### Phase 2: Bounded task-local expansion

- [x] Task 4: Track task inventory and reject stale references ([details](phase-02-expansion.md#task-4-track-task-inventory-and-reject-stale-references)) (depends on 3)
- [x] Task 5: Resolve explicit L2 targets without silently clipping ([details](phase-02-expansion.md#task-5-resolve-explicit-l2-targets-without-silently-clipping)) (depends on 4)

### Phase 3: Explicit DeepSeek consumption

- [x] Task 6: Adapt ready packs to the existing DeepSeek request ([details](phase-03-gateway.md#task-6-adapt-ready-packs-to-the-existing-deepseek-request)) (depends on 4, 5)

### Phase 4: Offline CLI and acceptance

- [x] Task 7: Expose build inspect and budget views ([details](phase-04-cli-acceptance.md#task-7-expose-build-inspect-and-budget-views)) (depends on 3)
- [x] Task 8: Verify compatibility and document rollout ([details](phase-04-cli-acceptance.md#task-8-verify-compatibility-and-document-rollout)) (depends on 6, 7)

## Commit Plan

- **Commit 1** (after tasks 1–3): `feat(context): pack checked ranked source under token budgets`
- **Commit 2** (after tasks 4–8): `feat(context): expose bounded expansion and packed context delivery`

## Definition of Done

- Packs enforce declared limits against the locally counted normalized input, preserve canonical source/provenance, and return no dispatchable content for stale or insufficient input.
- Explicit L2 follow-ups are bounded, deduplicated, and improve aggregate input on a controlled replay-all comparison without dropping needed definitions.
- Offline query views and MockTransport prove compatibility; unchanged V1 stage/provider/CLI behavior passes the default offline suite.
- Mandatory docs checkpoint is complete; no roadmap edit, migration, new default network traffic, or change to orchestrator behavior.
