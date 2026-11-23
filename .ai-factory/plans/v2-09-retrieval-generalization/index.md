<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Retrieval Dependency Coverage and Generalization

Mode: ultra  
Plan ID: `v2-09-retrieval-generalization`  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-23

## Original Request

Plan ID: v2-09-retrieval-generalization
Plan Title: Retrieval Dependency Coverage and Generalization
Milestone: V2 - Context Intelligence Hardening

Dependencies:
- v2-01-code-indexer
- v2-02-repository-map
- v2-03-context-ranking
- v2-04-context-budget
- v2-05-context-cache
- v2-06-workflow-integration
- v2-07-token-telemetry
- v2-08-e2e-optimization

Goal:
Improve general dependency coverage and source-context
completeness without overfitting to known evaluation
holdouts or increasing token consumption unnecessarily.

Authoritative architecture:
- Preserve RULES.md and accepted ADRs.
- Preserve the minimal Python WorkflowOrchestrator.
- LangGraph remains deferred.
- Preserve ContextProvider and ContextPack contracts.
- Public CLI: wl.
- Canonical entrypoint: lean_weave:main.

Background:
V2 release validation detected a retrieval regression:
two required dependencies were missing compared with
one in V1.

The known holdout results must be treated as historical
evaluation evidence, not as development fixtures.

Critical evaluation discipline:

1. Do not use known holdout examples as training,
   tuning, implementation, or regression fixtures.
2. Do not inspect holdout labels to design ranking
   weights, special cases, or selection heuristics.
3. Do not hardcode behavior for known holdout symbols,
   filenames, queries, or repository structures.
4. Do not repeatedly evaluate candidate changes
   against the known holdout.
5. Preserve historical holdout results unchanged.
6. Develop and test using independent fixtures.
7. Reserve final generalization testing for a new,
   previously unseen holdout in a separate plan.
8. Do not claim improved holdout performance based
   on development-set results.

Before planning:
- Inspect the existing V2 retrieval architecture.
- Review existing development fixtures.
- Review generic dependency coverage guarantees.
- Identify structural limitations independently of
  known holdout examples.
- Preserve all existing public interfaces where possible.

Functional requirements:

1. Analyze source dependency coverage.

Evaluate whether retrieved source fragments include
the declarations and contextual information necessary
to understand and safely modify their behavior.

2. Implement general referenced-definition discovery.

Support bounded discovery of relevant definitions
referenced by selected source fragments.

Use available deterministic evidence:
- imported names
- module relationships
- AST references
- symbol index
- dependency graph

Avoid speculative or unverified references.

3. Improve import context preservation.

Define a general policy for including relevant
import declarations when extracted source fragments
depend on externally defined names.

Do not blindly include every import in every file.

4. Introduce bounded dependency closure.

Support configurable expansion:
- maximum dependency depth
- maximum selected definitions
- maximum dependency token budget
- maximum total context budget

5. Integrate with ranking and packing.

Coordinate dependency discovery with:
- ContextRetriever
- RepositoryMap
- ContextPacker
- ContextCache

Avoid implementing a second retrieval pipeline.

6. Preserve source provenance.

Every added dependency must provide:
- source file
- source range
- reason for selection
- dependency relationship
- estimated token cost

7. Handle ambiguity safely.

When a referenced definition cannot be resolved
reliably, report uncertainty instead of inventing
a dependency.

8. Handle budget conflicts.

If necessary dependency context exceeds the budget:
- report insufficient context explicitly;
- support bounded progressive expansion;
- avoid silent omission of essential information;
- do not bypass global token limits.

9. Ensure cache correctness.

Invalidate cached context when dependency
relationships or referenced definitions change.

10. Build independent development fixtures.

Cover generic situations such as:
- imported class definitions
- cross-module type references
- aliased imports
- relative imports
- source excerpts requiring import context
- ambiguous references
- circular dependencies
- dependency budget exhaustion

Create original fixtures independently of the
known holdout repositories and their identifiers.

11. Evaluate development-set quality.

Measure:
- required dependency recall
- missing dependency count
- context token size
- added dependency tokens
- retrieval latency
- context expansion frequency
- deterministic behavior

12. Compare against V1 and current V2 baselines
using development fixtures only.

Token-efficiency requirements:
- Prefer deterministic static analysis.
- No LLM-based dependency expansion.
- Avoid unnecessary full-file inclusion.
- Avoid indiscriminate dependency expansion.
- Preserve useful information per token.
- Measure the total effect on context size.
- Reject changes that create disproportionate
  token overhead without improving coverage.

Testing:
- Independent development fixtures
- Import resolution
- Referenced definitions
- Source provenance
- Dependency depth limits
- Budget enforcement
- Cache invalidation
- Worktree isolation
- Existing ContextProvider compatibility
- Existing workflow regression tests

Acceptance criteria:
1. Dependency selection is based on general rules.
2. Independent development fixtures demonstrate
   improved or preserved dependency coverage.
3. Context budgets remain enforced.
4. Context expansion remains bounded.
5. Existing V1/V2 tests pass.
6. No known holdout-specific special cases exist.
7. No changes are validated by repeatedly tuning
   against known holdout results.
8. All performance claims are supported by
   development-set evidence.
9. A new blind holdout is required for final
   generalization validation.

Out of scope:
- LangGraph
- MCP
- ACP
- Embeddings
- Vector databases
- LLM-based reranking
- Holdout-specific patches
- Release certification

Ultra planning requirements:
Generate independently testable phases with
explicit interfaces, test fixtures, budget policies,
and compatibility checks.

Preserve the known holdout as historical evidence.
Do not use it as an implementation target.

Save using exact plan ID:
v2-09-retrieval-generalization

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes (mandatory `/aif-docs` checkpoint)

## Roadmap Linkage

Milestone: "V2 - Context Intelligence Hardening"  
Rationale: Requested milestone label for retrieval hardening; `.ai-factory/ROADMAP.md` currently lists the broader open **Hardening** milestone and the completed **V2 - Context Intelligence** milestone, not this exact name. Leave the roadmap and both existing milestone states unchanged; do not auto-close either.

## Requirements Reconciliation

Authority: [RULES.md](../../RULES.md) and accepted [ADR-001](../../adr/ADR-001-component-boundaries.md), [ADR-002](../../adr/ADR-002-orchestrator-langgraph-vs-minimal.md), [ADR-003](../../adr/ADR-003-llm-provider-deepseek.md), and [ADR-004](../../adr/ADR-004-context-isolation-and-state.md) govern architecture and authorization. Original Request governs feature/evaluation scope. Existing contracts and `ARCHITECTURE.md` supply compatibility constraints; the roadmap is direction, not an implementation contract. Verified use-site evidence extends, but does not replace, existing module-graph hints or change rank weights. A definition outside frozen read/emit scope is not a proven deliverable requirement.

| Decision / supported combination | Source and invariant | Verification evidence |
|---|---|---|
| **Default** — V1 provider, injected build-only provider, disabled V2, or L0 planning | Request architecture; RULES; ADR-001/002/004. Existing inputs, prompts, checkpoint data and authorities unchanged; no analysis I/O. | Tasks 3, 6, 7: disabled wire/ranking parity, ordinary CLI/contract and sealed L0 tests. |
| **Enabled** — packed L1 preview, approved V2 implement or attributed retry | FR 1–6; ADR-004. Checked snapshots only; mandatory anchor and proven closure before map; optional source+closure atomic; existing state unchanged. | Tasks 2–3, 6: exact spans, enabled `wl` surfaces, workflow seal and no broadened reads/writes. |
| **Uncertainty** — ambiguous, unsupported, external/unresolved or off-scope | FR 7; RULES. Lookup uses the full checked read universe; output filters may restrict emission but cannot manufacture uniqueness. No guessed target or denied path disclosure. | Tasks 2–3: binding negatives, collision outside emit scope, ready-but-incomplete distinction. |
| **Limits** — proven additions exceed mandatory vs optional budget | FR 4, 8; ADR-003/004. Mandatory refusal is non-dispatchable; optional group omitted as a unit. Use a stable conservative dependency sub-budget and existing wire/output caps. | Tasks 3–4, 6: dual estimator, typed refusals, no-send/no-reservation, unchanged fallback. |
| **Expansion** — live ready session with explicit registered selector vs failed initial pack | FR 8–9. Verify exact proof and fit on delivery; enabled workflow stages explicit roots and commits coverage/accounting only after a guarded repack. Failed initial build yields no receipt. | Tasks 4, 6: receipt, drift/limits, refusal rollback and next guarded round. |
| **Scope/cache** — cache off/cold/warm, local edit/collision vs sibling worktree | FR 5, 9 and ADR-004. Reuse only checked path-local facts; live dependency proof is not persisted. No new read authority or staged source. | Tasks 4–6: stale receipt, invalidation, real worktree separation, resume authority. |
| **Evaluation** — frozen development set; known holdout excluded | Original Request discipline, FR 10–12 and acceptance. Real V1 implement reference and current V2 packer control; no claim about sealed-workflow token/latency, release readiness or blind results. | Tasks 1, 6–7: score delivered intervals, separately verify sealed workflow and tracked CLI wiring, audit regression inputs. |

## Architecture and Decisions

- Prerequisites: `v2-01-code-indexer`, `v2-02-repository-map`, `v2-03-context-ranking`, `v2-04-context-budget`, `v2-05-context-cache`, `v2-06-workflow-integration`, `v2-07-token-telemetry`, `v2-08-e2e-optimization`. Depend on the implemented seams, not historical evaluation data.
- Structural gap: the V2 ranker can bring adjacent files into consideration but doesn't resolve the names used by selected fragments. Packing materializes definitions and ranks optional source separately; a standalone definition loses declaration context. Existing L2 dependency selection can choose the first definition of a related file without use-site proof. None of these observations relies on known holdout examples.
- Add one opt-in packing policy. ContextRetriever still owns scan/ranking; RepositoryMap supplies scoped static import/symbol facts; ContextPacker makes bounded selection and reports provenance; ContextCache still reuses checked reusable metadata only. Do not create a new ranking pipeline, persistent facts schema or tool. The blind-evaluation plan is a separate deliverable, not a blocking implementation question here.

## Phase Index

1. [Phase 1: Independent development baselines](phase-01-development-baselines.md) — Task 1
2. [Phase 2: Static evidence and closure](phase-02-static-evidence.md) — Task 2
3. [Phase 3: Budgeted packing and progressive expansion](phase-03-packing-and-expansion.md) — Tasks 3–4
4. [Phase 4: Cache correctness and product boundaries](phase-04-cache-and-boundaries.md) — Tasks 5–6
5. [Phase 5: Development-only quality gates](phase-05-development-gates.md) — Task 7

## Cross-Phase Dependencies

- Task 2 depends on Task 1 to freeze the independent oracle and baseline before product changes.
- Task 3 depends on Task 2 for exact closure evidence; Task 4 depends on Task 3 for the shared fit and provenance rules.
- Task 5 depends on Tasks 3–4 to verify cache parity through initial and expansion consumers. Task 6 depends on Tasks 4–5 to expose only revalidated context.
- Task 7 depends on Tasks 1 and 6 for baseline provenance and final production integration.

## Tasks

### Phase 1: Independent development baselines
- [x] Task 1: Freeze fixtures and baseline adapters ([details](phase-01-development-baselines.md#task-1-freeze-fixtures-and-baseline-adapters))

### Phase 2: Static evidence and closure
- [x] Task 2: Resolve referenced definitions from admitted evidence ([details](phase-02-static-evidence.md#task-2-resolve-referenced-definitions-from-admitted-evidence)) (depends on 1)

### Phase 3: Budgeted packing and progressive expansion
- [x] Task 3: Integrate dependency closure into L1 selection ([details](phase-03-packing-and-expansion.md#task-3-integrate-dependency-closure-into-l1-selection)) (depends on 2)
- [x] Task 4: Extend existing bounded expansion and receipts ([details](phase-03-packing-and-expansion.md#task-4-extend-existing-bounded-expansion-and-receipts)) (depends on 3)

### Phase 4: Cache correctness and product boundaries
- [x] Task 5: Prove fresh context on warm cache and worktrees ([details](phase-04-cache-and-boundaries.md#task-5-prove-fresh-context-on-warm-cache-and-worktrees)) (depends on 3, 4)
- [x] Task 6: Preserve workflow and CLI contracts ([details](phase-04-cache-and-boundaries.md#task-6-preserve-workflow-and-cli-contracts)) (depends on 4, 5)

### Phase 5: Development-only quality gates
- [x] Task 7: Gate recall and token cost ([details](phase-05-development-gates.md#task-7-gate-recall-and-token-cost)) (depends on 1, 6)

## Commit Plan

- **Commit 1** (after tasks 1–4): `feat(context): add bounded verified dependency packing`
- **Commit 2** (after tasks 5–7): `feat(context): integrate scoped dependency coverage and development gates`

## Definition of Done

- Checked deterministic evidence, exact provenance and conservative import retention; verified budget failures are explicit and non-dispatchable.
- Existing public interfaces, defaults, workflow state/authority, and safe ordinary V1/V2 regressions remain intact. No new persistent context material.
- Independent development comparisons satisfy the coverage and cost gates; no known holdout identifiers, labels, content or results guide selection, testing or claims. Historical evidence is unchanged.
- Documentation covers opt-in, limits, diagnostics and recovery. Generalization validation and release certification remain out of scope.
