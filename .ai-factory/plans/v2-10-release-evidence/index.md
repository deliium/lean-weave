<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Clean Build Evidence and Blind Release Validation

Mode: ultra  
Plan ID: `v2-10-release-evidence`  
Branch: `main` (planning checkout unchanged; select this exact bundle explicitly)  
Created: 2026-11-23

## Original Request

Plan ID: v2-10-release-evidence
Plan Title: Clean Build Evidence and Blind Release Validation
Milestone: V2 - Release Readiness

Dependencies:
- v2-08-e2e-optimization
- v2-09-retrieval-generalization

Goal:
Produce reproducible release evidence for V2
using a clean committed source revision and an
independent blind evaluation holdout.

Do not modify accepted architecture decisions.

Before planning:
1. Inspect the existing V2 release evidence protocol.
2. Inspect wheel build and artifact binding logic.
3. Identify why previous evidence was collected
   from a dirty source tree.
4. Inspect existing benchmark infrastructure.
5. Preserve historical evaluation records.
6. Do not introduce new retrieval algorithms.

Workstream A: Clean source and artifact binding

1. Require a specific committed Git revision.
2. Verify source tree cleanliness before evidence
   collection.
3. Build release artifacts from that revision.
4. Prefer an isolated clean build worktree.
5. Keep generated evidence outside the source tree
   or in explicitly excluded artifact locations.
6. Record:
   - Git commit SHA
   - source tree status
   - build configuration
   - dependency lockfile identity
   - wheel SHA-256
   - evaluation configuration
   - benchmark dataset identity
   - tool versions
7. Bind all evidence records to the exact artifact
   and source revision.
8. Revalidate artifact identity before reporting
   release eligibility.
9. Reject mixed or incomplete artifact bindings.
10. Do not reuse old evidence as proof for a newly
    built artifact unless the protocol explicitly
    supports verified reuse.

Workstream B: Independent holdout

1. Preserve all existing known holdout results
   as historical evidence.
2. Treat previously inspected holdouts as unsuitable
   for a fresh unbiased release claim.
3. Create a new independent evaluation holdout.
4. Ensure its cases are not copied or adapted from
   known development or previous holdout fixtures.
5. Keep evaluation labels and expected dependencies
   hidden from implementation and tuning.
6. Freeze the candidate implementation and its
   configuration before the blind evaluation.
7. Run V1 and V2 under comparable conditions.
8. Include representative dependency-heavy tasks.
9. Measure:
   - required dependency coverage
   - missing required dependencies
   - Recall@K
   - Precision@K
   - context size
   - retrieval latency
10. Use the same source revisions and evaluation
    definitions for compared implementations.
11. Do not modify retrieval weights or algorithms
    after examining blind evaluation failures.
12. If the new holdout fails, preserve the result,
    report Not ready, and require a new development
    cycle followed by a fresh blind evaluation.

Workstream C: Workflow reliability

1. Run the complete offline regression suite.
2. Validate:
   - Python WorkflowOrchestrator
   - DeepSeek Gateway mocks
   - StateStore and resume
   - context retrieval
   - dependency selection
   - token budgets
   - Git worktree isolation
   - verification and retry
3. Preserve source and artifact identity across
   test and reporting stages.
4. Report failures without hiding regressions.

Workstream D: Live API evaluation

1. Live DeepSeek evaluation is optional unless
   explicitly required by approved release policy.
2. Require explicit operator authorization.
3. Require available credentials.
4. Enforce configured token and cost budgets.
5. Run controlled comparable tasks if authorized.
6. Record actual API usage.
7. Compare tokens per successfully completed task.
8. Include failed attempts and retries.
9. Distinguish live results from offline estimates.
10. If live evaluation is not authorized:
    - do not run it;
    - report Live: Not evaluated;
    - do not claim measured token savings;
    - preserve any release gate requiring live evidence.

Workstream E: Release decision

Produce:
- source commit identity
- wheel hash
- reproducible build evidence
- clean-tree verification
- independent holdout results
- V1/V2 comparison
- workflow regression results
- token usage evidence
- live evaluation status
- unresolved issues
- final release eligibility verdict

Release gate:
- All required deterministic tests pass.
- No unresolved blocking retrieval regressions.
- Clean committed source is verified.
- Artifact binding is complete.
- Required holdout checks pass.
- No safety or workflow regressions.
- Live evidence is handled according to explicit
  release policy.

Do not mark the release ready if required evidence
is missing or invalid.

Do not claim token savings without appropriate
measurements.

Constraints:
- No new orchestration framework.
- No LangGraph migration.
- No MCP or ACP integration.
- No retrieval tuning using evaluation holdouts.
- No fabricated or inferred live API results.
- No automatic publishing.
- No automatic release tagging.
- No unrelated implementation changes.

Acceptance criteria:
1. Release evidence is tied to a clean committed build.
2. Wheel hash and source revision are verifiably bound.
3. Independent holdout methodology is preserved.
4. Required regression tests pass.
5. Metrics distinguish estimates from measurements.
6. Release eligibility is determined by explicit gates.
7. Any missing evidence produces a truthful status.
8. Historical failures remain available for audit.
9. No unrelated architecture changes are introduced.

Deliverables:
- Reproducible build procedure
- Artifact binding manifest
- Blind evaluation protocol
- Evaluation results
- Updated release report
- Explicit Ready / Not ready verdict

Ultra planning:
Separate artifact reproducibility, independent
evaluation, workflow verification, and release
decision into independently auditable phases.

Save using exact plan ID:
v2-10-release-evidence

## Settings

- Testing: yes
- Logging: verbose, with release evidence redaction
- Docs: yes (mandatory `/aif-docs` checkpoint)
- Live: not authorized by this request; no calls during planning.

## Roadmap Linkage

Milestone: "V2 - Release Readiness"  
Rationale: User-specified evidence milestone; `.ai-factory/ROADMAP.md` has no exact entry. Do not edit that file or close the completed Context Intelligence milestone or the separate Hardening milestone.

## Requirements Reconciliation

Authority: Original Request governs evaluation scope and release gates; project rules and accepted ADRs govern architecture and operational authority. v2-08 defines historical provenance and workflow protocol; v2-09 defines independence and the enabled dependency coverage setting. Existing code is evidence of current capabilities, not policy authority.

| Decision / supported combination | Sources and relationship | Verification |
|---|---|---|
| Clean pinned detached source + two matching builds + verified installed package can supply evidence. Source pytest is a distinct bound stage; dirty, mixed, unknown, or wheel-path-only reports cannot certify. | A/C; `docs/benchmarks.md` V2 comparison protocol; `conditions.py::release_binding` is weaker than the request. | Tasks 1–2/7: actual wheel and committed inputs rechecked; substitution rejected. |
| Known holdout never tunes or certifies. Full offline suite still runs: migrate only historical-dependent test inputs to original synthetic/development cases, preserving test behavior and historical records. | B.1–4/C.1; v2-09 “Critical evaluation discipline”; existing `tests/benchmarks/test_retrieval*.py` exercise known cases. | Tasks 3–5: audit automatic regressions; sealed label-free capture; retain v2-08 historical report unchanged. |
| Fresh blind paired L1 vs implement source from identical pinned fixture revisions; enabled V2 for dependency coverage; fallback/workflow evidence separate. Missing/failed/disclosed suite cannot qualify. | B.5–12; v2-09 dependency config and evaluation boundaries; existing installed stock variants do not enable the policy. | Tasks 3–4: per-case required spans and recalls, sizes and timing; no retuning, preserve failure and require new holdout. |
| Green source tests and installed scripted matrix validate different surfaces; both required. | C; v2-08 workflow thresholds and `docs/benchmarks.md`; tests use source paths. | Task 5: complete pytest node inventory and installed paired workflow including repair. |
| Approved optional live distinguishes verified no dispatch, inconclusive usage with independent safety evidence, and blocking regression. Required live needs qualified, authorized, complete evidence. | D/E; v2-08 Phase 3 Tasks 7/9; current runner has qualification gaps. | Tasks 6–7: no-network spies and decision matrix; no estimate substituted for actual usage and no missing-evidence waiver. |

## Architecture and Decisions

- Dependencies: [v2-08-e2e-optimization](../v2-08-e2e-optimization/index.md) and [v2-09-retrieval-generalization](../v2-09-retrieval-generalization/index.md); both implementation ledgers are complete. Neither delivered a ready release. Planning inspected clean `f352c223ece5fce3e6cf7d2532f2b77930aeb8eb`; this is not the future release pin.
- Historical v2-08 collection ran from the uncommitted v2-08 implementation over parent `dcde3b42…`; the digest can be reconstructed from the tracked edits and names of then-untracked additions in `91fc072`. This does not establish their untracked bytes or link the historical wheel to them. The old report's post-fix verdict and §3 show two V2 misses vs one V1; §6 records the earlier four-miss observation. §5.2 records a wheel hash with `source_tree_dirty`; §7's “no wheel hash” statement is stale. Do not overwrite either observation or promote v2-09 development results to blind evidence.
- Keep accepted ADRs, Python state machine, DeepSeek gateway, StateStore, Git isolation, `ContextProvider` and `ContextPack`, `wl`, and `lean_weave:main` unchanged as architectural boundaries. V1 remains the default. New tooling is developer/operator-only; no algorithm or weight changes, MCP/ACP, LangGraph, automatic publishing or tagging.
- A verified artifact manifest and independently pinned run ID join create-once receipts from one preregistered attempt. Standalone `compare` is not release authority. Real evidence statuses are separate from tooling checkbox completion.
- Build, scorer, regression and decision tooling must all be committed **before** the real candidate build, freeze and run. Check off tooling only after synthetic verification. Then the operator runs Tasks 1–2 on the selected commit; freezes the run ID and executes Tasks 3–5; records Task 6's live disposition; and invokes Task 7 even when earlier evidence is missing. Real evidence and reports stay external; a later docs pointer names the evaluated revision without rebinding it.

## Operational Release Blockers

- The operator must select a full committed candidate OID after tooling is committed, provide a pinned offline toolchain and test environment, and freeze the approved evaluation and release policy. A custodian independent of implementation must provide a new sealed holdout. Missing any required input yields **Not ready**, not an invented result. These are execution prerequisites, not open implementation decisions.
- No live authorization or qualified live run is established. Current live runner gaps are documented in Phase 3; optional skips report **Live: Not evaluated**. If approved policy requires live, qualification and valid live results are mandatory.

## Phase Index

1. [Phase 1: Artifact Reproducibility](phase-01-artifact-reproducibility.md) — Tasks 1–2
2. [Phase 2: Independent Blind Evaluation](phase-02-independent-evaluation.md) — Tasks 3–4
3. [Phase 3: Workflow Verification and Live Disposition](phase-03-workflow-verification.md) — Tasks 5–6
4. [Phase 4: Release Decision and Audit](phase-04-release-decision.md) — Task 7

## Cross-Phase Dependencies

- Tooling: Task 2 depends on Task 1's build-proof contract. Task 3 depends on Task 2's run binding contract. Task 4 depends on Tasks 2–3's verifier and freeze contracts. Task 5 depends on Task 2's verifier; Task 6 depends on Tasks 2 and 5's verifier and offline receipt contracts. Task 7 depends on Tasks 2, 4, 5 and 6's schemas. None requires a real release receipt to implement.
- Operator execution only: after both tooling checkpoints, Task 2 verifies the real artifact and freezes the approved run ID with Task 3's preregistration inputs. Once the run is frozen, Task 5 can run alongside blind capture/scoring. Task 6 always records a disposition; billable evaluation requires Tasks 4 and 5 to pass plus approved admission. Task 7 consumes available receipts and records missing/failed stages rather than waiting for a pass.

## Tasks

### Phase 1: Artifact Reproducibility
- [x] Task 1: Build twice from a clean commit ([details](phase-01-artifact-reproducibility.md#task-1-build-twice-from-a-clean-commit))
- [x] Task 2: Bind evidence to the installed wheel ([details](phase-01-artifact-reproducibility.md#task-2-bind-evidence-to-the-installed-wheel)) (depends on 1)

### Phase 2: Independent Blind Evaluation
- [x] Task 3: Seal the holdout and freeze rules ([details](phase-02-independent-evaluation.md#task-3-seal-the-holdout-and-freeze-rules)) (depends on 2)
- [x] Task 4: Capture and score the frozen pair ([details](phase-02-independent-evaluation.md#task-4-capture-and-score-the-frozen-pair)) (depends on 2, 3)

### Phase 3: Workflow Verification and Live Disposition
- [x] Task 5: Verify the offline workflow ([details](phase-03-workflow-verification.md#task-5-verify-the-offline-workflow)) (depends on 2)
- [x] Task 6: Record live policy and measured usage ([details](phase-03-workflow-verification.md#task-6-record-live-policy-and-measured-usage)) (depends on 2, 5)

### Phase 4: Release Decision and Audit
- [x] Task 7: Enforce release eligibility ([details](phase-04-release-decision.md#task-7-enforce-release-eligibility)) (depends on 2, 4, 5, 6)

## Commit Plan

- After Tasks 1–4 tooling and synthetic tests: `feat(benchmark): add clean build and blind evidence tooling`
- After Tasks 5–7 tooling and synthetic tests: `feat(benchmark): enforce V2 release evidence gates`
- Operator chooses a committed candidate after both checkpoints. No tag, push or publication step.

## Definition of Done

- Reproducible clean-wheel procedure and binding manifest, custodian-controlled blind protocol/results, offline and live disposition receipts, and a machine-verifiable release decision with a matching human report.
- Evidence is bound to one verified artifact and revision. Missing, invalid or failed required evidence yields **Not ready**. Optional unauthorized live is **Live: Not evaluated**, with no measured savings claim.
- Historical failures remain auditable; no exposed holdout becomes a tuning or regression fixture. Scope and architecture remain unchanged.
