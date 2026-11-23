# Phase 3: Budgeted packing and progressive expansion

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Phase 2 / Task 2

## Objective

Admit whole, useful dependency groups through the existing V2 packer, with observable limits and a non-dispatchable failure when essential proof does not fit.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/packer.py` | `_compose`, `_select`, `_assemble`, `_fit_failure` | Live `rank_checked` callback owns source selection; current order is required source → map → optional source. |
| `src/lean_weave/context/packing_models.py` | `PackedContextV2`, `PackResult`, `ExpansionRequest` | Only `ready` can contain messages; keep existing selectors and version. |
| `src/lean_weave/context/packer.py` | `_resolve_expansion`, `_delta_for_target`, `prepare_expansion_delivery` | Old dependency selector resolves a file edge to its first definition; receipt and carried context are checked independently. |
| `src/lean_weave/context/packing_session.py` | `PackingSession` | Only ready results open the existing ephemeral session. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/config/models.py` | modify | Add opt-in policy and strict bounds. |
| `src/lean_weave/context/packing_models.py`, `packing_source.py`, `packer.py` (under `src/lean_weave/context/`) | modify | Integrate analyzer, exact closure fit, report metadata and safe rematerialization. |
| `src/lean_weave/context/packing_session.py` | modify | Ephemeral, revision-bound receipts and dependency counters. |
| `tests/config/test_context_config.py`, `tests/context/test_packer.py`, `tests/context/test_packing_expansion.py`, `tests/context/test_packing_tokens.py` | extend | Opt-in, budget, provenance and exact receipt regressions. |

## Task 3: Integrate dependency closure into L1 selection

### Intent
Prefer dependencies of selected source over unrelated optional matches without globally retuning retrieval.

### Implementation Steps
1. Add a sibling `context.dependency_coverage` config (`enabled: false`, `max_depth: 2` in `0..4`, `max_definitions: 8` in `0..32`, `max_tokens: 1024` in `0..8192`); strict integers, reject booleans/coercions. Zero is a real bound. Keep `context.packing.total_tokens` as the total-context authority; the effective dependency ceiling is `min(max_tokens, source_tokens, total_tokens - effective_output_tokens)`. Existing data defaults, imports at module scope, no model or cache side effects when disabled.
2. In `_select`, keep required-anchor precedence. On enabled L1 build the full binding graph via `scoped_graph_for_scan` over the same checked read scan, **before** applying emit scope; use the `rank_checked` final witness, not a second scan. Add a scan-local full-facts accessor to `PackingCatalog` (accepting only scan-member snapshots) that shares its fact cache/batch and memo by `(path, digest)`; leave existing public `facts(path)` behavior intact. Filter source materialization by emit scope as in Task 2. Do not replace the ranking or map-rendering graph. L0 bypasses analysis. Resolve and fit normalized mandatory roots **and proven closure before map rows**. Then run existing map selection. For each optional source trial stage candidate plus its newly needed closure and fit atomically; if it does not fit, skip the whole trial. Keep existing optional rank order.
3. Fit each trial through existing `_assemble` and `_fit_failure` with exact section, normalized wire, reserved output and existing character/byte limits. Independently pin dependency estimates to `ConservativeByteCounter.count_text(render_source([unit]))` using the same exact source/ref and canonical `definition` or `range` header even when delivered as a delta. This sub-cap has one stable estimator per policy across compose, expansion, delivery and repack; `_assemble` still applies its existing adapter/fallback for ordinary wire caps. Report the sub-cap estimator separately. Count one addition per revision-bound ref, with only newly added definition units counting toward `max_definitions`. Required/ranked/explicit **root** text is not charged as an automatic addition unless that ref was already charged; prior charges are never reclassified or refunded. Use exact interval coverage for legitimate already-emitted roots; no artificial containing range to bypass a charge, and no splitting across definition/import boundaries.
4. For mandatory verified work blocked by count/depth/token/section/total/character/cumulative/work limits or exact materialization, use `insufficient_context` with `dependency_limit:<finite-dimension>` or `dependency_unavailable`, no messages or dispatch; do not fall back or silently drop known requirements. When a binding is unresolved rather than proven, return explicit `dependency_uncertain` and stable subreason with the originating admitted interval, no guessed target; readiness may remain `ready` for useful verified context, but `coverage_complete=false`. Distinguish unsupported analysis/work cap from successful empty coverage; a work cap preventing analysis of a mandatory supported anchor is `insufficient_context`. An incomplete scan may not prove a missing/unique target; preserve existing stale/scan guard and report uncertainty or existing fail-closed refusal, never speculate. An optional omission also marks incompleteness, without making an otherwise useful pack non-ready.
5. Add opt-in-only V2 metadata on `PackedContextV2`: optional dependency summary (limits/status/counters, estimator and new charged tokens), evidence rows joining existing `FragmentRef`-style origin and target, reference line, relationship, reason, depth, estimated unit cost and new charge, and issues with finite reasons and emittable refs only. Keep existing fragment kind values (`range` for independent import statements), reason semantics and IDs; omit new fields at legacy projection boundaries. Multiple edges to one unit report the same estimated unit cost but at most one new charge; totals equal new charges. Failed packs may project safe reasons/counters, never candidate source or dispatchable messages. Use `dependency_limit:{depth,definitions,tokens,source_section,total,per_file_chars,total_chars,cumulative,analysis_work}` for verified cap failures; preserve existing non-dependency reason codes.

### Required Interfaces and Contracts
- No change to `ContextProvider`, `ContextPack`, retrieval scores/results, map CLI, gateway wire shape or version 2 request schema. New fit policy stays behind L1 selection.
- All reference intervals are inclusive; an import or definition row is emitted only when the ready output covers its full verified interval or explicitly marks it omitted. The selected text comes from the checked snapshot, not metadata or cache.

### Requirement Evidence
- Original Request FR 1–9 and token-efficiency requirements; index combinations **Default**, **Enabled**, **Uncertainty**, **Limits**.

### Error Handling and Logging
- DEBUG counts for selected dependency/import units, charged tokens, cap dimensions, and uncertainty; no source bodies, query content, hidden paths or prompts. Distinguish disabled from verified empty and refusal. Stale and invalid config follow existing typed boundaries.

### Tests
- Controlled small budgets exercise each independent cap, two-level closure, order before map, ready optional skip, atomic mandatory refusal, uncertainty vs empty, covered fragments at zero added cost, overlapping/repeated edges, multiline imports, envelope output reserve and counter fallback. Snapshot the disabled L1/L0 wire and compare ranking hits unchanged; run no-provider tripwire.

### Acceptance Criteria
- Proven mandatory closure either fits all existing and added caps or the result cannot dispatch; enabled evidence is exact and legacy defaults are unchanged.

### Verification
- `uv run --locked pytest tests/config/test_context_config.py tests/context/test_dependency_coverage.py tests/context/test_packer.py tests/context/test_packing_tokens.py`

## Task 4: Extend existing bounded expansion and receipts

### Intent
Let callers request useful follow-up source without sending under-budget incomplete groups or using L2 as a budget escape.

### Implementation Steps
1. When enabled for L1, run the same analyzer and fit on explicit registered symbol/range expansions and their new closure. Root depth is measured from the explicit user-selected seed for that request; already-emitted refs are not charged again. Legacy file-level `dependency` edge requests may resolve only to one exact analyzer-backed referenced target from the anchor path/revision; when zero or multiple targets match, return `dependency_unavailable` with a stable uncertainty reason, not the existing arbitrary first definition. An admitted exact symbol selector is the disambiguation route; no new selector syntax. Disabled sessions retain legacy expansion semantics byte-for-byte.
2. Freeze read paths, the original read predicate, emit scope, policy, revisions and caps in the enabled session. Conjoin any additional filter with the original; it may reject delivery but must not establish new uniqueness by hiding a previously admitted competitor. Revalidate proof against the original checked read universe; if it cannot be checked, refuse. Determine carry IDs from exact prior fragments covering the selected root or its proven closure; include complete carry plus delta in both fit and delivery. `expand` reserves unique additions, cumulative source units and attempts once when creating a valid receipt; `prepare_expansion_delivery` rechecks limits without double charge. Workflow uses the staged transaction in Task 6 instead of treating a receipt preview as sent. Preserve exact import refs on rematerialization using `_rematerialize`'s checked slice, with explicit evidence metadata rather than generic delta reasons.
3. Bind receipt dependency proof to the scope/policy, originating revisions, target refs, proof algorithm version, and exact resolved relationships. Re-run the same proof on a fresh checked scan before emission at delivery; validate all carried/delta refs, including lookup metadata for collision or relative resolution changes even if the original target bytes match. Stale closes session; incomplete verification refuses without sending. Keep only refs, counters and proof metadata in session, no new text or persistence. Keep exact existing carry-ID/envelope requirements. For enabled receipt delivery report the same relationship/cost attribution and reasons as the checked result, not synthetic generic source.
4. No session opens for an initial `insufficient_context`. Recovery is an explicit narrower L1 rebuild (or operator-approved config change) through the same ceilings, never auto-retry, auto-increase, or partial-send. For a ready pack, callers can use at most the configured number of explicit expansion attempts. Run proof before `no_change`: a root already covered with newly required closure is not a no-op. A bare selector already covered **and** requiring no new context returns `no_change`; a blocked group returns `insufficient_context` with no new receipt or coverage. In enabled mode a file-edge proof must have the correct direction and a recorded origin fragment; do not reinterpret reverse or unanchored edges as forward use evidence.

### Required Interfaces and Contracts
- Keep `open_task`, `open_scoped`, `expand`, `prepare_expansion_delivery` signatures usable as today; enrich internal receipt metadata only. Failed recognized attempts may consume an attempt but do not commit fit/accounting/coverage deltas. Enabled replay still rechecks source, scope and exact approval.

### Requirement Evidence
- Original Request FR 4, 6–9; index combinations **Limits**, **Expansion**, **Scope/cache**.

### Error Handling and Logging
- Preserve invalid/unregistered request errors, stale statuses and no-change semantics. DEBUG expansion counts, delivered/new cost, blocked dimension, receipt verification outcome; no receipt IDs, replay, source text or denied paths.

### Tests
- Use original local trees to assert exact L2 symbol and import delivery, ambiguous or reverse edge refusal, exact rematerialization, changed importer/target or duplicate module insertion, mutation at final witness check, already-covered root with new dependency, unknown/closed receipt, incorrect carry/envelope, recognized limit failures/no receipt, combined carry+delta over-budget, narrower-filter collision tripwire, stable dependency estimate through wire counter fallback, and disabled receipt parity.

### Acceptance Criteria
- Progressive expansion is explicit, exact, bounded, and verified at delivery; an unready initial build never becomes dispatchable via a receipt.

### Verification
- `uv run --locked pytest tests/context/test_packing_expansion.py tests/context/test_packing_session.py tests/context/test_dependency_coverage.py`

## Phase Risks and Mitigations
- New proofs could consume all optional space: mandatory closure precedes map and optional source, with independent hard sub-caps and frozen dev controls.
- Independent token counts or cached slices could drift from actual wire: count the original admitted source and normalized whole request on each acceptance.

## Phase Completion Checklist
- Verify Tasks 3–4 separately and update each checkbox in `index.md`.
