<!-- aif:plan-mode:ultra -->
# Ultra Implementation Plan: Token-Efficient Context Lifecycle Integration

Plan ID: `v2-06-workflow-integration`  
Mode: ultra  
Branch: `main` (planning checkout unchanged; select this bundle explicitly for implementation)  
Created: 2026-11-22

## Original Request

Plan ID: v2-06-workflow-integration
Plan Title: Token-Efficient Context Lifecycle Integration
Milestone: V2 - Context Intelligence

Dependencies:
- v1-06-orchestrator
- v1-07-worktree-state
- v1-08-e2e-validation
- v2-01-code-indexer
- v2-02-repository-map
- v2-03-context-ranking
- v2-04-context-budget
- v2-05-context-cache

Goal:
Integrate the V2 Context Engine into the existing minimal Python WorkflowOrchestrator without changing the approved orchestration architecture.

Authoritative decisions:
- ADR-002 defines the MVP orchestrator.
- ADR-004 governs approved state and context isolation.
- RULES.md defines project conventions.
- LangGraph remains deferred under v3-01-langgraph-orchestrator.

Critical architecture requirement:
Extend the current deterministic Python state machine.

Do not replace it with LangGraph.
Do not build another workflow engine.
Do not create an additional agent orchestration layer.

Before planning:
1. Inspect the actual v1-06 implementation.
2. Identify stage handlers and transition policies.
3. Inspect FeatureState and checkpoint contracts.
4. Inspect existing ContextProvider.build behavior.
5. Inspect DeepSeek Gateway and tool-calling mechanisms.
6. Identify context duplication and unnecessary LLM calls.
7. Preserve approved interfaces wherever possible.

Functional requirements:

1. Context Engine integration

Connect:
- CodeIndex
- RepositoryMap
- ContextRetriever
- ContextPacker
- ContextCache

Use existing ContextProvider interfaces or compatible adapters.

2. Stage-specific context

Implement context selection for:
- planning
- implementation
- verification
- diagnosis
- retry

Each stage must receive only the information required for its task.

3. State separation

- Keep persistent FeatureState separate from LLM messages.
- Preserve existing StateStore and checkpoint semantics.
- Never serialize full conversations as durable workflow state.
- Do not persist DeepSeek reasoning buffers.
- Preserve structured task progress and result metadata.
- Avoid unnecessary propagation of state fields.

4. Planning context

- Use compact repository maps.
- Include relevant interfaces and dependencies.
- Avoid unrelated implementation details.
- Preserve task requirements and constraints.

5. Implementation context

- Load code for the active implementation task.
- Include relevant source definitions.
- Include required dependencies and constraints.
- Avoid sending the entire feature plan when a task-level specification is sufficient.
- Support context expansion when necessary.

6. Verification and retry context

When verification fails:
- Identify relevant failing checks.
- Extract bounded diagnostics.
- Identify affected files and symbols.
- Include current changes when needed.
- Reuse existing task state.
- Avoid repeating unrelated context.
- Preserve prior failed-attempt information only when it is necessary to avoid repeating mistakes.

7. Context expansion

- Support bounded symbol and source-range requests.
- Prevent repeated requests for identical fragments within the active task.
- Enforce cumulative token and tool-call budgets.
- Detect stale source references.
- Rebuild context after relevant file modifications.

8. Workflow compatibility

Preserve:
- deterministic transitions
- bounded retries
- token budgets
- user approvals
- safe resume
- interruption handling
- cancellation
- worktree isolation
- terminal result semantics

Do not alter the stage model unless required for compatibility with an approved interface.

9. Fallback strategy

- Allow V1 Context Builder behavior as a compatibility fallback.
- Support configuration-based selection where useful for benchmarking.
- Ensure fallback does not bypass safety or budget policies.

10. Observability

Record:
- context fragments selected
- context estimates
- context expansion count
- retrieval duration
- cache reuse
- repeated content avoided
- stage-level input token usage

Do not log full source code, secrets, or prompts.

Token-efficiency requirements:
- Minimize cumulative LLM input across the feature.
- Avoid duplicate repository exploration.
- Avoid unnecessary context replay between stages.
- Keep active tool-call history only as required by API semantics.
- Do not introduce LLM-based context summarization by default.
- Stop additional LLM calls when deterministic verification succeeds.
- Preserve correctness and adequate source context.

Testing:
- Simple feature
- Multi-file feature
- Planning bypass
- Context expansion
- Verification failure and retry
- Source changes during execution
- Budget exhaustion
- Interrupted execution and resume
- Review mode
- Stale approval rejection
- V1 fallback compatibility

Acceptance criteria:
1. V2 Context Engine works through the existing Python orchestrator.
2. No LangGraph runtime dependency is added.
3. Existing FeatureState and StateStore contracts remain valid.
4. Each LLM stage receives task-specific context.
5. Resume does not unnecessarily repeat completed stages.
6. Context expansion is bounded and validated.
7. Existing safety and verification tests continue to pass.
8. Token consumption remains measurable.

Out of scope:
- LangGraph migration
- Multi-agent execution
- Nested OpenCode sessions
- MCP or ACP
- Background workers
- New checkpoint frameworks
- Complete orchestrator redesign

Save using the exact plan ID:
v2-06-workflow-integration

## Settings

- Testing: yes
- Logging: verbose
- Docs: yes
- Rollout: V1 default; explicit V2 selection in trusted product configuration.

## Roadmap Linkage

Milestone: "V2 - Context Intelligence"  
Rationale: This plan integrates the completed V2 context primitives into feature execution; the already checked roadmap milestone is a scope link, not evidence that integration is shipped.

## Requirements Reconciliation

Authority: [ADR-002](../../adr/ADR-002-orchestrator-langgraph-vs-minimal.md), [ADR-004](../../adr/ADR-004-context-isolation-and-state.md), and [RULES.md](../../RULES.md) are authoritative architecture/policy; Original Request defines the new behavior. Dependency contracts remain valid. The preview-only statements in the earlier V2 plans are delivery boundaries for those plans; this request adds a distinct workflow opt-in, not a change to preview or V1 defaults. The v1 token guide's metadata-only planning is refined to admit scoped interface/map metadata in V2, not source bodies. Verification remains model-free.

| Supported combination / persisted behavior | Requirement or existing authority | Verification evidence |
|---|---|---|
| **V1 default or build-only injection:** existing context build/wire, authority digest and state; no V2 construction/I/O. Effective V2 is explicit, frozen on tracked nonterminal runs; terminal reads ignore execution config. | Request §§1, 3, 8–9; v1-06 and v1-07; ADR-004 | Tasks 1, 4, 7: exact legacy baseline, config/authority drift and no-I/O tests. |
| **V2 auto, review, and bypass:** PLAN gets exact request, trusted constraints, approved targets and a scoped map; live baseline drift refuses plan/gate publication; review rejects stale approval and gates all implement effects; typed bypass validates before checkpoint. | Request §§2, 4, 8, Testing; v1-06/v1-07, ADR-002 | Tasks 1–4, 7: during-plan edit, source-open tripwires, review/stale/approval and bypass. |
| **V2 implement and retry:** active-task source, bounded deterministic dependencies/diagnostics; no unrelated source, plan or transcripts. Verify has only existing local approved check inputs; pass/nonblocking warning ends, strict unique attribution retries, ambiguity/infrastructure/exhaustion never reaches repair. Store existing task/verification/result fields only. | Request §§2–6, 8; v1-06 `repair_attribution`; ADR-004, RULES stage isolation | Tasks 2–4, 7: multi-file, genuine fail→repair→pass and zero post-success calls. |
| **Healthy/missing/stale index or cache:** just checked fact reuse/live degradation within the same scoped scan; stable empty map valid in plan. Only explicit fallback mode and sole safe pre-send no-anchor reason can use V1 rendering for implement, with the same scope and caps; stale, incomplete, authority, guard and budget failures never fall back. | Request §§1, 7, 9; v2-01–05 admission and proofs | Tasks 1–4, 7: scoped opens, cold/warm equivalence and refusal matrix. |
| **Read, duplicate, own patch, external drift:** approved symbol/range may add unique live refs; duplicates add no source. Count one raw edit revision per editable path; confirmed own edits rebuild without resetting caps. Unproven drift or uncertain patch stops; a later local refusal retains prior calls, usage and guard semantics. | Request §§3, 5, 7–8; v2-04, `WorkflowTools`, checkpoint envelope | Tasks 3–5, 7: initial CRLF patch without a read, exact replay, late refusal and pending/settled edit cases. |
| **Safe resume, interrupted effect, cancel:** reload only saved structured state; no repeat of a completed stage; no reconstruction/replay of unresolved effects; existing lifecycle creates all terminal outcomes. | Request §§3, 8 and Acceptance; v1-07; ADR-002/004 | Tasks 4–5, 7: fresh process/session and unresolved-marker/terminal tests. |
| **Stage costs:** compare the same gateway journal input estimate in V1 and V2; measure preparation, reuse and dedup independently of reported provider input; no prompt telemetry or duplicate charge. | Request §10 and token efficiency; RULES; v1-03 ledger and v1-08 paired protocol | Tasks 6–8: paired real `benchmarks/fixtures/manifests/multi_file_feature.json` with request-derived revisions, redaction and usage coverage. |

## Architecture and Decisions

- Extend `MinimalWorkflowOrchestrator._plan_stage` and `_run_tool_stage`; keep `_drive`, `ready_task`, `TRANSITIONS`, public provider/store APIs and checkpoint schemas in control. Diagram: `FeatureSession → existing orchestrator → private stage context adapter → scoped scan/index facts/map/retrieval/packing + optional cache → existing tool loop/budget → existing DeepSeek gateway`. Diagnosis remains a deterministic helper in the verify/retry path. [v3-01](../v3-01-langgraph-orchestrator.md) remains deferred.
- Planning uses L0 approved interface metadata, implementation and retry use scoped L1, and existing approved read tools opt into task-local L2. Intersect executor read policy and repository scope before any source body open, independently of ranking/pack output. The counted raw revision supports the existing patch precondition but never grants write authority.
- Measured waste to address: broad V1 plan digests in tool stages, repeated task fact work, unused import selection in retry, replay of old base source, and duplicate read results. Preserve replay required by DeepSeek; do not claim fewer calls by treating a patch as task completion. Never add planning on bypass, diagnosis calls, gateway retries, LLM summarization or sends after verification succeeds.
- Reuse existing effect guards, reservations, journal and retry limits; do not treat cache reuse as token savings. Default V1 remains deployable without migrations. No blocking open questions; tests gate implementation and rollout.

### Completed dependencies

- Workflow: [v1-06-orchestrator](../v1-06-orchestrator.md), [v1-07-worktree-state](../v1-07-worktree-state.md), [v1-08-e2e-validation](../v1-08-e2e-validation.md).
- Context: [v2-01-code-indexer](../v2-01-code-indexer/index.md), [v2-02-repository-map](../v2-02-repository-map/index.md), [v2-03-context-ranking](../v2-03-context-ranking/index.md), [v2-04-context-budget](../v2-04-context-budget/index.md), [v2-05-context-cache](../v2-05-context-cache/index.md).

## Phase Index

1. [Phase 1: Trusted selection and scoped V2 inputs](phase-01-scoped-context.md) — Tasks 1–2
2. [Phase 2: Stage projections and guarded sends](phase-02-stage-requests.md) — Tasks 3–4
3. [Phase 3: Current-task expansion and observability](phase-03-expansion-observability.md) — Tasks 5–6
4. [Phase 4: Offline acceptance and rollout](phase-04-acceptance.md) — Tasks 7–8

## Cross-Phase Dependencies

- Task 2 depends on Task 1 for trusted effective mode and worktree root.
- Task 3 depends on Tasks 1–2 for selection and scoped source. Its retry scope projection supplies the requested path set at runtime to the earlier scoped input primitive.
- Task 4 depends on Task 3 for final stage input; execute their integration verification together.
- Task 5 depends on Task 4 for guarded round composition; Task 6 depends on Task 5 for expansion/reuse metrics.
- Task 7 depends on Task 6 for paired evidence; Task 8 depends on Task 7 for verified operator documentation.

## Tasks

### Phase 1: Trusted selection and scoped V2 inputs
- [x] Task 1: Bind context selection to workflow authority ([details](phase-01-scoped-context.md#task-1-bind-context-selection-to-workflow-authority))
- [x] Task 2: Build a scoped scan and map path ([details](phase-01-scoped-context.md#task-2-build-a-scoped-scan-and-map-path)) (depends on 1)

### Phase 2: Stage projections and guarded sends
- [x] Task 3: Project context for each existing stage ([details](phase-02-stage-requests.md#task-3-project-context-for-each-existing-stage)) (depends on 1, 2)
- [x] Task 4: Seal and charge the exact wire request ([details](phase-02-stage-requests.md#task-4-seal-and-charge-the-exact-wire-request)) (depends on 3)

### Phase 3: Current-task expansion and observability
- [x] Task 5: Bound expansion and rebuild after edits ([details](phase-03-expansion-observability.md#task-5-bound-expansion-and-rebuild-after-edits)) (depends on 4)
- [x] Task 6: Record bounded context and stage telemetry ([details](phase-03-expansion-observability.md#task-6-record-bounded-context-and-stage-telemetry)) (depends on 5)

### Phase 4: Offline acceptance and rollout
- [x] Task 7: Prove paired workflow acceptance ([details](phase-04-acceptance.md#task-7-prove-paired-workflow-acceptance)) (depends on 6)
- [x] Task 8: Document opt-in and finish regression gates ([details](phase-04-acceptance.md#task-8-document-opt-in-and-finish-regression-gates)) (depends on 7)

## Commit Plan

- **Commit 1** (after Tasks 1–4): `feat(workflow): compose guarded V2 context for existing stages`
- **Commit 2** (after Tasks 5–8): `feat(workflow): add bounded context expansion and paired acceptance`

## Definition of Done

- Existing Python driver and checkpoint contracts remain authoritative; no new stage, graph dependency, state migration, write/check authority or persistent replay.
- Scoped V2 plans, implementation, and repairs reach the existing gateway with the evidence each needs; local checks and diagnosis issue no model calls.
- Expansion, staleness, tool replay, dual budgets, review, safe continuation and V1 compatibility pass isolated offline regression tests.
- Paired fixture oracles and stage-level estimates demonstrate correctness and measurable input cost. Report measured reductions only with matching conditions and coverage.
- Mandatory docs checkpoint and `uv run --locked pytest` pass.
