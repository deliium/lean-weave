# Implementation Plan: Minimal Feature Development Orchestrator

Plan ID: v1-06-orchestrator  
Branch: none created (plan-only changes on `main`)  
Created: 2026-11-20

## Original Request

Plan ID: v1-06-orchestrator
Plan Title: LangGraph Feature Development Orchestrator

Dependencies:
- v1-01-architecture
- v1-02-project-foundation
- v1-03-deepseek-gateway
- v1-04-context-builder
- v1-05-code-tools

Goal:
Implement an autonomous but bounded feature development workflow using LangGraph.

Required workflow:
1. Prepare context.
2. Generate implementation plan when necessary.
3. Validate the plan.
4. Implement one task at a time.
5. Run deterministic verification.
6. Diagnose failures.
7. Retry with bounded attempts.
8. Finalize results and metrics.

Requirements:
1. Implement FeatureState using approved typed schemas.
2. Build a LangGraph StateGraph with explicit nodes and transitions.
3. Integrate DeepSeekProvider.
4. Integrate ContextProvider.
5. Integrate safe code editing tools.
6. Implement an instruction loader for reusable AI Factory planning and implementation methodologies.
7. Allow selective loading of relevant skill instructions.
8. Do not invoke AI Factory skills as independent nested agents.
9. Support structured implementation plans with small executable tasks.
10. Define deterministic transitions between planning, implementation, verification, and retry.
11. Allow simple tasks to bypass detailed planning when safe.
12. Implement bounded LLM tool-calling loops.
13. Implement retry logic based on compact verification errors.
14. Persist task progress through the existing state abstraction.
15. Enforce per-task token and LLM-call budgets.
16. Implement explicit success, failure, and budget-exhausted states.
17. Provide structured workflow results.

Token-efficiency requirements:
- Each stage receives only required FeatureState fields.
- Do not forward complete previous LLM conversations between stages.
- Use compact task specifications.
- Rebuild context deterministically when possible.
- Do not repeatedly send unrelated repository information.
- Stop immediately when acceptance criteria are satisfied.
- Measure cumulative tokens across planning, implementation, and retries.

Constraints:
- Single orchestrator.
- No multi-agent hierarchy.
- No nested OpenCode execution.
- No MCP server.
- No background workers.
- No unbounded autonomous loops.
- No destructive actions outside approved workspace tools.

Acceptance criteria:
- A small feature can be implemented automatically.
- Tests run after implementation.
- Verification failures trigger bounded retries.
- The workflow terminates with an explicit status.
- Token budgets are enforced.
- End-to-end tests work with MockLLMProvider.

Save the plan using the exact identifier:
v1-06-orchestrator

## Settings

- Testing: yes; offline by default.
- Logging: verbose, controlled by `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`); metadata only.
- Docs: yes; mandatory `/aif-docs` checkpoint after implementation, outside the product workflow.
- Dependencies: [v1-01-architecture](v1-01-architecture.md), [v1-02-project-foundation](v1-02-project-foundation.md), [v1-03-deepseek-gateway](v1-03-deepseek-gateway.md), [v1-04-context-builder](v1-04-context-builder.md), [v1-05-code-tools](v1-05-code-tools.md).

## Roadmap Linkage

Milestone: "Minimal WorkflowOrchestrator"  
Rationale: Implement the deterministic feature loop and the persistence and adapter work it requires; public feature CLI wiring remains separate.

## Requirements Reconciliation

Authority: Approved ADRs and [RULES.md](../RULES.md) take precedence over previous plans and the original request where it conflicts. The 2026-11-20 clarification confirms [ADR-002](../adr/ADR-002-orchestrator-langgraph-vs-minimal.md) for MVP 1. The original request is retained as history, not as authorization for a runtime dependency. `StateGraph`, LangGraph interrupts, and LangGraph checkpointing are deferred to [v3-01-langgraph-orchestrator](v3-01-langgraph-orchestrator.md). If MVP adoption is reconsidered, propose an ADR amendment with justification, cost, and alternatives and obtain approval first.

| Combination | Required behavior and persistence | Evidence |
|---|---|---|
| Explicit simple request or generated plan | Bypass the planning call only with a caller-supplied, bounded single-task spec, one approved existing file, and approved deterministic checks. Both paths validate a `FeaturePlan` before writes; invalid plans fail closed. | ADR-002; `schemas/models.py`; mock tests for zero planning calls, normal planning, and rejection before writes. |
| Verification passes, warns, or fails | Pass or nonblocking warning with every required gate satisfied → `DONE`. A repairable blocking finding mapped to one approved task → `RETRY`; ambiguous, unauthorized, or infrastructure failure → `FAILED`. No later LLM call after final verification passes. | State schema reference, `TRANSITIONS`, `VerificationRunner`; branch and call-count assertions. |
| Retry or budget limit reached | Retry limits are cumulative across the run; task token and call limits carry across attempts and resume. Deny the next unaffordable dispatch. If reported spend exceeds the allowed amount after a call, stop before any further dispatch or tool action. Expose `budget_exhausted` separately while retaining compatible `FeatureState.stage`. | Usage ledger, gateway admission, ADR-004; pre-dispatch refusal, overage, and restart tests. |
| New start, duplicate start, terminal resume, or uncertain effect | A duplicate start is rejected; terminal resume returns the saved state and result without LLM calls, checks, or edits. A clean nonterminal resume reconstructs context and revalidates approvals. An unresolved effect, including a committed but durability-unconfirmed patch, interrupts with the marker intact and is not replayed. | `StateStore`/`WorkflowOrchestrator` protocols, `executor/patch.py`, [ADR-004](../adr/ADR-004-context-isolation-and-state.md); crash, unconfirmed commit, and stale-workspace fixtures. |
| Authorized or unauthorized tool action | Only caller-approved ops, current task files, and fixed check IDs are eligible. Plan text, YAML, and tool responses cannot grant authority. Neither skills nor repository content can dispatch processes. | `executor/approval.py`, `executor/patch.py`, `verify/checks.py`; copied real `src/lean_weave/app.py` round trip and scope/approval rejection tests. |
| Direct check or check that runs workspace code | A trusted external check may read workspace files as data. A required check that executes workspace code needs the caller-supplied `IsolationBackend`; without it the workflow refuses before editing. Never fall back to direct execution. | `verify/checks.py::run_checks`, `verify/process.py::IsolationBackend`; no-backend/no-write test and offline parser/assertion fixture. |
| Verification failure with eligible retry | Persist `Stage.RETRY`, a failed `VerificationResult`, and the uniquely selected active task before rebuilding retry context. Only bounded, allowlisted diagnostic codes and approved task-relative locations are eligible for the prompt. | `context/provider.py::_retry` and `build`; real-provider prompt assertions and ambiguous-diagnostic refusal. |

## Design

- Keep `FeatureState`, `LLMProvider.complete`, `ContextProvider.build`, `CodeExecutor.run`, `VerificationRunner.run`, `StateStore.load/save`, and `WorkflowOrchestrator.run_feature/resume` as the existing interfaces. Use the schema's `TRANSITIONS` as the baseline and test explicit terminal/error paths alongside it. Put stage functions and policies outside the driver; no graph objects or framework dependencies in the shared core.
- Run `PLAN (prepare → choose path → validate) → IMPLEMENT (one ready task at a time, stable plan order) → VERIFY → DONE | RETRY | FAILED`. `RETRY` builds a new stage-scoped context with compact diagnostics, repairs just the selected task, then re-verifies. Native `INTERRUPTED`/resume is a saved safe boundary, not a human-in-the-loop graph API.
- Keep schema v1.0.0 payloads readable. Use a private versioned checkpoint envelope for durable control metadata (task allowances/spend, pending effect marker, terminal reason and safe resume cursor); do not add chat or change the existing state payload. Expose a separate typed result projection with `success`, `failed`, `budget_exhausted`, or `interrupted`, reason code, task status, verification summary, and cumulative counters. Budget exhaustion projects from `Stage.FAILED` with its typed reason, not a new public stage value.
- Keep the public protocol string-in/state-out. Provide a separate typed library entry point on the concrete driver for the optional single-task spec, and a read-only result lookup by feature ID for both start paths. Caller approvals are separate from both inputs; descriptive request text never selects bypass.
- `WorkflowConfig` supplies bounded limits; caller approval supplies authority. For a Git workspace, require an already isolated caller-approved workspace or refuse automatic writes; this plan does not automate Git lifecycle, push, CLI feature commands, or commits. Checks that execute workspace code require both approval and a trusted `IsolationBackend`; cwd admission alone is not an OS sandbox.

## Commit Plan

- After tasks 1–3: `feat: add workflow state, plan policy, and approved tool adapters`
- After tasks 4–6: `feat: run the bounded feature loop with offline integration coverage`

## Tasks

### Phase 1: Durable state and trusted inputs

- [x] Task 1: Implement local checkpoints and typed results. Add `src/lean_weave/workflow/{state,results}.py` and `tests/workflow/test_state.py`. Back the existing StateStore protocol with contained, bounded, version-checked JSON under configured `.lean-weave/` state storage, atomic replacement, and a single-writer lease for each feature. Atomically store `FeatureState` and the private control record via an internal checkpoint capability without changing the public protocol; fail closed for durable execution if the injected store lacks that capability. Validate safe feature IDs and workspace identity, reject duplicate starts, corrupted files, symlinks, and incompatible records. Persist an effect marker before a call/edit/check and reconcile accounting on reload; if completion cannot be proven, return a structured interruption without replaying the effect. Test old state decoding, torn writes, competing writers, and resumed terminal results. Log state transitions and checkpoint versions at DEBUG, refusal/recovery codes at WARN.
  - Persist only scoped effect identity, relevant revision metadata, and accounting markers, not patch bodies, prompts, or raw check output. Clear markers only after confirmed outcomes are saved. An interruption for an unresolved effect is not permission to retry; an ordinary `resume` must return that interruption without dispatching.
- [x] Task 2: Implement methodology loading and plan validation. Add `src/lean_weave/workflow/{instructions,planning}.py` and `tests/workflow/test_planning.py`. Use a bounded, stage-keyed registry of project-owned excerpts distilled from [v1-01 methodology reuse](../references/v1-01-aif-methodology-reuse.md); load only explicitly selected fragments for the current stage, never arbitrary paths, tool declarations, whole skill trees, or nested commands. Implement pure plan request construction from the canonical context message pair and validation of either returned JSON or a normalized typed single-task spec. Do not call the provider here. Validate both paths for schema/version, bounded nonempty tasks, unique IDs, known acyclic dependencies, caller-approved existing patch targets, and pending-only task state. Treat model-owned settings as data, never as authority. Test unknown fragments, malformed output, cycles, scope violations, bypass eligibility, and deterministic ordering. Log selected fragment IDs, counts and validation codes, never excerpt text.
- [x] Task 3: Adapt standalone tools and deterministic checks without widening approvals. Add `src/lean_weave/workflow/tools.py`, `src/lean_weave/verify/runner.py`, and `tests/workflow/test_tools.py`. Use `ToolApproval` from explicit caller opt-in; bind typed read and revision-checked patch functions to fixed names, intersect patch targets with active task files, and cap inputs and tool results. Keep checks orchestrator-owned and map fixed approved check outcomes to existing `VerificationResult`. Where a `CodeExecutor` adapter is needed, only accept exact registry matches for root/argv/timeout/network and delegate to the existing bounded runner; no arbitrary argv, shell, automatic Git changes, or execution of model-supplied commands. Refuse missing required checks before writes. Test stale revisions, attempts to exceed task scope, denied commands, and distinction between repairable check failures and tool/config errors. Log operation IDs, verdicts, and output sizes, not payloads.
  - Preserve `apply_patch`'s `durability="unconfirmed"` as a committed-but-uncertain outcome, not a stale-patch response. Stop the batch, keep the effect marker, and do not mark the task done.
  - Preflight approved required checks and backend availability before edits; retain the same checks at execution time. Route workspace-code checks through `run_checks(..., backend=...)` with offline/read-only isolation; an adapter must not bypass that boundary. For retry attribution, translate only recognized diagnostic codes and validated locations. Blocked, timed-out, truncated, unrecognized, or ambiguously attributed failures do not authorize a model repair.

### Phase 2: Tight execution bounds and assembly

- [x] Task 4: Implement the bounded stage-local LLM loop and accounting (depends on 1–3). Add `src/lean_weave/workflow/{budget,tool_loop}.py`, limits in `src/lean_weave/config/models.py`, and `tests/workflow/test_budget.py`. Cap batches, rounds, plan calls, and per-task calls/tokens across retries; reserve conservative prompt plus output allowance before dispatch, including schemas and replayed turns. Disable gateway retries for workflow calls with `options.retry_limit=0` so a logical call cannot hide extra upstream attempts. Use the existing gateway ledger as the production source for feature-wide spend and reported totals; count uncertain attempts conservatively, never record gateway usage twice or charge cache/reasoning counters again. Test missing usage, failed attempts, budget refusal before dispatch, and equivalent preflight when using `MockLLMProvider`. Echo `reasoning_content` only within the current DeepSeek tool loop when required; drop its buffer at exit and never persist it. Log reservations, call/round counts, remaining budget, and exhaustion codes only.
  - Book the scoped call count and token reservation in the workflow checkpoint before invoking the provider. Share admission math with `metrics/budget.py` and use a fixed planning scope or the current task ID. Settle against verified usage, otherwise retain at least the reservation; never infer a task debit from feature/stage-only journal totals. Keep task budget debits separate from feature-wide reported metrics. If a returned completion or gateway error indicates budget overage, record the reason and stop before applying any returned tool action. Test incomplete usage, known overage, and crashes across booking, dispatch, and settlement; no interrupted call receives a refund or a second dispatch just because its outcome is missing.
- [x] Task 5: Implement and assemble the minimal driver (depends on 1–4). Add `src/lean_weave/workflow/{stages,orchestrator}.py` and a lazy wiring seam in `src/lean_weave/app.py` using the existing injected dependencies and provider factories. Persist progress at deterministic boundaries; derive resume from checkpoint and fresh approvals, not prior messages. On verification failure, select only a uniquely attributable task and pass bounded, deduplicated error codes/locations into a fresh retry context; never rerun successful unrelated tasks. Stop on passing required gates, explicit failure, interruption, or budget exhaustion. Test legal and illegal transitions, failed-dependency handling, ambiguous diagnostics, retry caps, and resume behavior with table-driven unit tests. Log stages and terminal statuses at INFO, retry reasons and guard refusals at WARN/DEBUG.
  - Expose concrete-driver `run_simple(feature_id, request, task_spec) -> FeatureState` and `result(feature_id) -> WorkflowResult` outside the protocol. Route the string-only detailed path through `ContextProvider.build("plan", ...)`, Task 2 request construction/validation, and Task 4's guarded JSON call without tools. An invalid explicit bypass input fails; it does not silently make a planning call.
  - Update `src/lean_weave/context/provider.py` and its retry tests without changing `build`'s signature. Before calling `build("retry", ...)`, persist the retry stage, failed verification result, and selected active task. Extend `_retry` to render capped, deduplicated blocker codes and locations intersected with eligible task paths, within the existing context budget. Build the retry call from that canonical pair, never from appended raw error text or earlier messages.
- [x] Task 6: Prove the full path offline and complete docs (depends on 5). Add `tests/workflow/test_feature_e2e.py`: copy real `src/lean_weave/app.py` into a temporary workspace as in `tests/executor/test_code_tools_integration.py`; use `MockLLMProvider` with the real context provider, state store, approved patch dispatcher, and an approved local test gate. Exercise valid plan and bypass, edit/test/pass, one repair then pass, exhausted retries, task/feature budget exhaustion, and process-restart recovery. Assert no cross-stage messages, no calls after success/refusal, no out-of-scope edits, and cumulative usage attribution across stages. Run `uv run pytest` including the existing dependency-policy suite. Update `docs/architecture.md` and `docs/getting-started.md` at the docs checkpoint with the library entry point, approval and sandbox limits, and result statuses. Assert fresh logs expose only metadata and bounded reason codes.
  - Use an approved external parser/assertion check that reads the copied module as data, emits a recognized workspace-relative diagnostic on failure, and never imports or executes the edited file. Separately exercise the no-backend refusal for a required workspace-code check, clean restart versus committed-unconfirmed interruption, actual retry prompt contents, and structured result lookup after resume.
