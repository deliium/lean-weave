# Implementation Plan: Git Worktree Lifecycle, CLI Integration and Workflow Control

- Plan ID: v1-07-worktree-state
- Branch: `main` (plan only; no branch or worktree created)
- Created: 2026-11-20

## Original Request

Plan ID: v1-07-worktree-state
Plan Title: Git Worktree Lifecycle, CLI Integration and Workflow Control
Milestone: MVP 1 - Core Coding Pipeline

Dependencies:
- v1-01-architecture
- v1-02-project-foundation
- v1-03-deepseek-gateway
- v1-05-code-tools
- v1-06-orchestrator

Goal:
Expose the existing feature development orchestrator through the public CLI and provide safe Git worktree isolation, execution modes, and lifecycle management.

Authoritative decisions:
- ADR-002: MVP 1 uses a minimal deterministic Python state machine.
- ADR-004: Preserve approved context isolation and state contracts.
- RULES.md: Preserve project architecture and naming.
- v1-06-orchestrator owns workflow execution, checkpoints, budget tracking, safe resume, and core approval enforcement.
- v3-01-langgraph-orchestrator is deferred.

Project conventions:
- Project: Lean Weave
- Python package: lean_weave
- Canonical entrypoint: lean_weave:main
- Public CLI executable: wl

Before planning:
1. Inspect the actual v1-06 implementation.
2. Identify existing StateStore, WorkflowOrchestrator, WorkflowConfig, FeatureState, and ToolApproval implementations.
3. Reuse existing checkpoints and resume capabilities.
4. Identify missing CLI integration points.
5. Avoid duplicating existing workflow functionality.
6. Preserve existing tests and approved interfaces.

Functional requirements:

1. Git worktree management

Implement a WorktreeManager that:
- Creates isolated worktrees for feature execution.
- Uses collision-safe feature identifiers.
- Creates dedicated Git branches where appropriate.
- Supports existing approved worktrees.
- Preserves the original working tree.
- Never automatically merges changes.
- Never automatically commits user changes.
- Detects invalid, conflicting, or stale worktree paths.
- Prevents concurrent mutation of the same worktree.
- Validates repository and worktree identity.
- Supports inspection and explicit safe cleanup.

2. Workflow integration

- Integrate WorktreeManager with the existing WorkflowOrchestrator.
- Construct the approved workspace before allowing writes.
- Pass explicit workspace identity and caller approvals.
- Preserve existing StateStore and checkpoint contracts.
- Do not introduce another orchestration engine.
- Do not create an independent retry loop.
- Do not duplicate token budget accounting.
- Ensure resumed execution uses the correct worktree.

3. Public CLI

Implement or complete:

- wl feature <description> --auto
- wl feature <description> --review
- wl status <feature-id>
- wl resume <feature-id>
- wl cancel <feature-id>
- wl result <feature-id>

Reuse the existing CLI application and entrypoint.

Do not create a second CLI implementation.

4. Automatic mode

- Execute the complete feature workflow without intermediate plan confirmation.
- Require explicit invocation with --auto.
- Restrict tool access to approved project policies.
- Do not interpret automatic mode as unrestricted authorization.
- Reject actions exceeding configured permissions.
- Preserve existing token, retry, and tool-call limits.

5. Review mode

- Pause at a safe pre-implementation boundary.
- Present the generated plan before code modifications.
- Require explicit user approval.
- Persist approval decisions when necessary for safe resume.
- Validate plan revision, worktree identity, and authorized scope.
- Reject stale approvals after relevant changes.
- Reuse existing approval and checkpoint mechanisms.

If v1-06 does not provide a suitable approval boundary,
introduce the smallest compatible extension rather
than implementing another workflow or StateStore.

6. Resume and cancellation

- Use WorkflowOrchestrator.resume.
- Preserve existing checkpoint semantics.
- Never replay uncertain file mutations.
- Allow cancellation at safe execution boundaries.
- Preserve worktree changes on failure or cancellation.
- Return structured status and failure reasons.

7. Results

Provide:
- Feature identifier
- Current workflow status
- Worktree path and branch
- Modified files
- Git diff summary
- Verification summary
- LLM usage summary
- Failure or interruption reason
- Next permitted action

8. Testing

Add offline tests for:
- Worktree creation and isolation
- Existing uncommitted user changes
- Conflicting branches and worktree paths
- Duplicate feature execution
- CLI automatic mode
- CLI review approval
- Stale approval rejection
- Cancellation
- Resume after interruption
- Terminal state retrieval
- Safe handling of failed operations

Architecture constraints:
- Do not introduce LangGraph.
- Do not replace the Python state machine.
- Do not duplicate JSON checkpointing.
- Do not recreate FeatureState.
- Do not duplicate token budgets.
- Do not add MCP or ACP.
- Do not add background workers or Docker.
- Do not introduce automatic Git merge, reset, or clean operations.

Acceptance criteria:
1. wl feature executes a feature inside an isolated Git worktree.
2. Automatic and review modes function correctly.
3. User approvals are explicit and bounded.
4. Existing workflow checkpoints are reused.
5. Resume works without restarting completed LLM stages.
6. Failed tasks preserve recoverable changes.
7. Original working tree remains unchanged.
8. CLI and integration tests pass offline.
9. No LangGraph runtime dependency is introduced.

Deliverables:
- WorktreeManager
- CLI workflow commands
- Approval and lifecycle integration
- Offline tests
- Usage documentation
- Updated architecture documentation

Generate an implementation-ready plan with explicit
tasks, file changes, dependencies, and acceptance tests.

Save using the exact plan ID:
v1-07-worktree-state

## Settings

- Testing: yes; offline.
- Logging: verbose, controlled by `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`); structured reason codes and counts only, no descriptions, plan prose, raw Git/check output, credentials, or file contents in logs.
- Docs: yes; mandatory `/aif-docs` checkpoint.
- Dependencies: [v1-01-architecture](v1-01-architecture.md), [v1-02-project-foundation](v1-02-project-foundation.md), [v1-03-deepseek-gateway](v1-03-deepseek-gateway.md), [v1-05-code-tools](v1-05-code-tools.md), [v1-06-orchestrator](v1-06-orchestrator.md). The existing ContextProvider, introduced in v1-04, is reused through v1-06.

## Roadmap Linkage

Milestone: "Thin CLI"

Rationale: This delivers public workflow commands and the worktree binding needed for safe execution under the requested MVP 1 - Core Coding Pipeline umbrella; it does not close the separate live end-to-end or hardening milestones.

## Requirements Reconciliation

Authority: [ADR-002](../adr/ADR-002-orchestrator-langgraph-vs-minimal.md), [ADR-004](../adr/ADR-004-context-isolation-and-state.md), and [RULES.md](../RULES.md) constrain the current request. The delivered v1-06 contracts govern execution and persistence. The LangGraph wording in v1-06's historical request was superseded by its reconciliation section; [v3-01](v3-01-langgraph-orchestrator.md) remains deferred. The roadmap is scope guidance, not an API specification.

| Combination | Validation, persistence, and observable behavior | Source / acceptance evidence |
|---|---|---|
| New managed worktree or explicit adoption | Start from resolved local `HEAD` for a new branch; adopt only a clean, attached, explicitly selected linked worktree in the same repository, never the origin checkout. Persist registration before execution. Preserve the origin even if dirty. | Request §1–2; ADR-004; real Git tests for dirty origin, adopted checkout, foreign/busy/stale identity. |
| Missing/both flags or invalid policy | Reject before provisioning for invalid mode; missing/unsafe permissions or check backend fail before LLM dispatch or source writes, with recoverable registration if provisioning already occurred. | Request §3–5; v1-05 caller approvals and v1-06 check preflight. |
| Repository config or explicit operator config | Repository config cannot authorize public feature execution. Start and nonterminal execution resume require an explicitly selected external operator config. Inspect, result and cancel remain available without it. | Request §2, §4, §6; `config/loader.py` and `cli/runtime.py`; missing/malformed config, checked-in config, and approved external config fixtures. |
| Auto or review | Auto uses bounded caller opt-in, not model/YAML authority. Review saves the validated plan and pauses before implementation; only an explicit matching approval token unlocks writes. No token, wrong token, or stale binding does not dispatch or replace the plan. | Request §4–5; check call counts and untouched content before valid approval; reject altered plan, policy or workspace. |
| Clean interruption or pending effect | Resume the saved continuation with the same approvals, store and spend; pending effects override normal resume and remain blocked with their markers intact. | Request §6; v1-06 `PendingEffect`; restart at PLAN, IMPLEMENT, VERIFY and RETRY; no replay of uncertain effects. |
| Idle, active or terminal cancellation | Idle cancellable runs finalize under the worktree lease. Active runs receive a durable cancel request and stop cooperatively; no signal kills or rollback. Already terminal is unchanged. Pending uncertain effects require inspection rather than being cleared. | Request §6; race and process-death tests, checks of durable terminal reason and retained edits. |
| Read-only queries or explicit cleanup | Status/result do not initialize execution dependencies or write state. Cleanup only removes an eligible clean managed worktree after explicit confirmation; no adopted checkout or automatic branch removal. | Request §1, 7; bounded diff fixtures, no-write queries, refusal and cleanup tests. |

## Implementation Baseline

Paths below are relative to `src/lean_weave/` unless prefixed with `tests/` or `docs/`.

- `contracts/state_store.py::StateStore` is `load/save`; `workflow/state.py::JsonCheckpointStore` supplies the private `CheckpointEnvelope`, workspace identity, spend, per-save lease and effect markers. `schemas/models.py::FeatureState` and `config/models.py::WorkflowConfig` already own schema and limits. Keep `FeatureState` and the generic protocols unchanged.
- `contracts/workflow_orchestrator.py::WorkflowOrchestrator` exposes `run_feature` and `resume`; `workflow/orchestrator.py::MinimalWorkflowOrchestrator` also has `result`. Today `resume` drops `decision`, interruptions lose the original stage, and legacy `INTERRUPTED → IMPLEMENT` cannot safely resume every stage. Use existing `workflow/results.py::project_result` with additive CLI projection, not a second state machine.
- `executor/approval.py::approve_tools` freezes `ToolApproval` for a root. `app.py::create_orchestrator` checks store/approval root equality, but factories default to CWD. `PlanPolicy` uses exact paths while `ToolsConfig.write_allow` supports patterns. `verify/runner.py::preflight_checks` requires a bounded nonempty registry and an injected `IsolationBackend` for workspace-code checks.
- `cli/commands/feature.py` is a help-only group; `cli/app.py` owns Typer and error rendering, and `cli/runtime.py::command_context` owns lazy config. `executor/git_diff.py` rejects a linked worktree gitfile; do not relax that general tool to make CLI status work.

## Design Contracts

### Ownership and location

- Resolve the source repository from the invocation directory with Git; require an existing local commit and a supported, trusted repository layout. No Git initialization, pull, fetch, or no-Git fallback for `wl feature`; existing non-feature commands and library paths still work without Git. Produce a strict ID from a UUID4 hex value (no description in IDs), validate with `normalize_feature_id`, and use `feature/<id>` for managed branches. Pin the full local source commit before provisioning. Never stage, stash, or copy uncommitted source changes or credentials.
- Store a bounded versioned **routing record only** in a private, no-follow `lean-weave/` directory under the validated Git common directory: ID, canonical source/worktree identities, branch, base commit, ownership, provisioning phase and frozen relative state directory. No plan, progress, budgets, or workflow checkpoint copy. Use an owner-only sibling worktree parent outside the source checkout, keyed by repository identity. The actual `JsonCheckpointStore` and shared workflow/gateway usage journal remain per worktree under the contained configured state directory. Exclude control paths from context and tools and reject a base/adoption with colliding pre-existing state paths. Use existing atomic/private IO conventions; missing or corrupt routing never permits guessing/rebinding.
- Serialize routing allocation across cooperating processes and hold an OS-released, exclusive lease keyed by validated repository plus linked-worktree administration identity across start/resume/idle cancel/cleanup, including provisioning handoff. Use nonblocking acquisition with typed busy outcomes. Reserve ID, path and branch before spawning; preserve diagnosed partial operations and never blindly delete leftovers. Require matching worktree registration, canonical top level, common directory, admin link back-pointer, directory identity and branch; reject stale or ambiguous entries. External Git/editor operations do not honor Lean Weave locks: revalidate identity at execution boundaries and fail closed on observed drift; document that concurrent manual edits are unsupported while a run holds the lease.
- `inspect` is read-only. `wl worktree cleanup <feature-id> --confirm` takes the same lease and refuses primary, adopted, active, pending-effect, stale or dirty worktrees, including ignored/untracked user files and submodule content. Never force-remove or delete the branch. For a terminal managed run, validate the checkpoint first, then move only recognized app-owned state artifacts into an owner-only archive outside the worktree; unknown files block cleanup. Journal archival as a routing phase so a failed removal is explicit. Keep result readable through the existing bounded checkpoint decoder with the original registered workspace identity as the expected identity; archived state is read-only and never eligible for execution. Clean, never-started managed worktrees are also eligible. Retain a routing tombstone after successful removal. For stale provisioning or unavailable state that cannot be positively classified, report manual recovery rather than guessing.
- For removal eligibility, use a separate, complete local cleanliness probe that includes ignored and untracked entries. Treat any parse failure, output cap, skipped entry, or other incomplete result as unknown and refuse removal. Before archiving, classify only the registered feature's known state files as app-owned; an unexpected file, symlink or directory blocks cleanup. Recheck for all remaining changes before non-forced removal. The archived reader must check feature ID and both stored workspace identities against the registration, and must not synthesize identity for an unwrapped legacy payload. Do not use the redacted status view as removal evidence.

### CLI and authority

- Replace the help-only feature registration with a command on the existing Typer root: `wl feature DESCRIPTION (--auto | --review) [--worktree PATH]`. Bare `wl feature` shows help without loading config; `--worktree` means explicit adoption, never arbitrary cwd selection or an override for `resume`. `wl resume ID [--approve TOKEN]`, `wl status ID`, `wl cancel ID`, `wl result ID`; `wl worktree inspect ID` and explicit cleanup above. No interactive prompt or generic `--yes`; review prints the plan and exact approval command. `--approve` is valid only at the outstanding review gate. Preserve shared entry points and existing operational/usage/config exit conventions (`1/2/3`); successful review pause, inspection and accepted cancel requests exit `0`.
- Load trusted operator config once from the invocation context; do not `chdir` or reload it from the target. Pass an explicit worktree root to context, store, tool admission, check cwd and ledger wiring. Preserve default CWD behavior for existing callers. Map project-relative custom config paths into the target's protected paths; also protect routing, state and Git administration data. Use trusted caller selection of the configured check IDs; neither the plan nor the LLM may specify executable/argv. For the CLI use a bounded deterministically enumerated intersection of `write_allow` with existing regular, policy-admitted files at start; fail on incomplete enumeration. Freeze exact targets/checks and limits (both plan policy and tools enforce them); never grant a new file or check on resume. Flag invocation plus validated operator policy provides caller opt-in; review permits planning but the driver independently gates effects. Public CLI can run approved external data-only checks; workspace-code checks remain blocked without a trusted, injected backend. A worktree is not a process sandbox.
- For public start or nonterminal execution resume, require root `--config PATH` to explicitly name an operator-controlled file whose resolved path is outside the source checkout, Git administration directory, and target worktree. Validate provenance before granting execution authority; never fall back to the CWD default for these commands. The operator attests to the executable **and its arguments** as data-only; checking that an executable is external or that YAML says `runs_workspace_code: false` is not proof. Document that config must be trusted and that interpreters or test runners loading repository code require the injected backend. Keep existing non-feature and library config defaults. Status, result, cancel, inspect, cleanup and terminal resume use routing and the frozen state location; they bypass `command_context` and must work without execution config, key, provider, or check runner.
- Bind the review token to canonical validated plan bytes, feature ID, managed/adopted worktree identity, pinned HEAD, normalized policy/config/limits and a bounded deterministic baseline fingerprint of admitted planning inputs and approved file bytes. Use the same bounded/no-follow inventory policy for the fingerprint; refuse indeterminate inputs instead of claiming a complete fingerprint. Save the gate and plan in one checkpoint at the PLAN→IMPLEMENT boundary, with no implementation call, patch or check. Verify token and recompute binding under the lease immediately before atomically accepting. After acceptance, keep the immutable plan/scope/identity binding; use current checkpoint effect and revision guards, not the pre-implementation whole-file fingerprint against edits made by the workflow itself. Revalidate identity/HEAD and frozen effective policy for all mutating resumes; changed policy or stale review requires a new feature, not silent widening or replanning. Do not put the token or routing metadata in model prompts.
- Define the canonical plan projection explicitly: version, goal, validated settings, reconciliation and commit metadata, and ordered task IDs, titles, descriptions, files and dependencies. Exclude mutable task `status` and `outcome_digest` (`schemas/models.py::ImplementationTask`). Capture the baseline before planning dispatch, compare it again before publishing the gate, and reject drift instead of silently adopting new bytes. Fingerprint only the approved paths and actual admitted metadata inputs; this local guard must not expand what the PLAN prompt reads or contains.

### Control and projection

- Extend the private checkpoint control record with versioned optional lifecycle fields for mode, plan/policy binding, decision and a validated continuation cursor. Read existing records with legacy behavior where provable; ambiguous legacy interruptions fail closed, while completed legacy results stay readable. Do not invent a new `FeatureState` stage. Add a typed internal resume decision consumed by existing `resume` and a cancellation boundary hook; preserve direct-caller defaults and `run_simple`.
- Capture the last validated safe stage and task progress before interruption; restore it via the existing transition driver, preserving task outcomes and reservations. Use `pending_effect` as the highest-priority no-dispatch guard. Handle cancellation before a new external effect and after its outcome is durably confirmed; never consume a marker, clear spend or replay a partial mutation to honor a cancel. Safe cancellation is terminal (`FAILED` in the existing schema, typed `cancelled` in results); `resume` returns it unchanged.
- Today `run_tool_loop` ends the final LLM effect before the driver saves the accepted plan or task outcome, and `_verify_stage` clears its marker before saving the verification result. Keep a recoverable guard until the existing checkpoint atomically saves the stage outcome, transition or terminal reason, settled spend/usage, and marker clearance. If process loss precedes the outcome checkpoint and no durable outcome can prove completion, retain the interruption guard and do not redispatch just because the stage still says PLAN, IMPLEMENT, RETRY or VERIFY. Do not persist provider messages or tool transcripts. If a crash loses an incomplete tool loop after a confirmed edit but before task completion, do not treat a cleared patch marker alone as permission to restart that task; preserve the edits and project a manual-inspection outcome unless the durable cursor proves a safe continuation.
- Check cancellation before budget reservation/provider dispatch, before each tool dispatch (not just each round) and before verification. Once an effect starts, settle it and durably account for its outcome before honoring cancellation; stop the batch and carry accumulated usage back to the driver instead of throwing it away. Do not use exceptions from `WorkflowTools` patch callbacks as the cancellation signal: `_apply_patch` catches those as marker failures. If the cancellation races with an unconfirmed effect, keep the effect guard and report inspection required.
- Use a small, feature/run-bound atomic cancellation marker beside trusted routing metadata, published by `cancel` when the execution lease is busy. Never write the feature checkpoint from that competing process. Bind markers to feature/workspace and keep requests durable across owner loss until consumed or classified. If idle, acquire the lease and use the same driver-owned finalization path without constructing a provider or dispatching effects. If completion wins the race, terminal result wins; status reports a request as pending, not final. No background process or forced signal delivery.
- Extend `project_result` at the CLI boundary with feature ID, status, safe reason, branch/path, modified paths, bounded staged/unstaged/untracked Git status summary (not a patch or attribution), required verification summaries, cumulative saved usage, and a deterministic next action. Use a separately validated linked-worktree Git read capability; disable optional locks and bound/redact path output. Distinguish no changes, filtered/incomplete data, and unavailable/stale worktree. Never dump the checkpoint or raw Git output. After cleanup, report archived terminal state and unavailable live diff; busy or pending cancellation comes from lease/marker facts, not guesses based on Stage alone.

## Commit Plan

- After tasks 1–4: `feat: add isolated worktree lifecycle and checkpoint controls`
- After tasks 5–8: `feat: expose feature workflow commands and offline acceptance coverage`

These are suggested developer checkpoints, not product operations or authorization to commit.

## Tasks

### Phase 1 — Git lifecycle and durable controls

- [x] Task 1: Implement the fixed Git adapter in `worktree/git.py` and focused tests in `tests/worktree/test_git.py`. Reuse `verify/process.py::run_bounded` with trusted absolute Git, minimal environment, fixed argv, NUL-delimited worktree/status parsing, and bounded time/output. Define and test fail-closed checkout preflight for repository-local executable configuration (e.g. hooks, filters, fsmonitor), sparse/submodule layouts not supported by MVP, config includes and remote/lazy object fetching; apply trusted no-hook/no-prompt/no-network settings, not just the read-only context Git settings. Support local identity, listing, provisioning, non-forced remove and bounded read-only status only; never expose raw argv to the LLM. Tests: unsupported or truncated metadata, hostile inherited environment, non-execution canaries, no source-index refresh. Logging: DEBUG operation code/count; WARN typed refusal or bounded-process error. (Request §1, ADR-004.)
- [x] Task 2: Implement `WorktreeManager` in `worktree/{__init__,manager,registry}.py` with `tests/worktree/test_manager.py` (depends on 1). Implement reservation, provisioning, explicit adoption, validated lookup/inspection, leases and cleanup per Ownership and location. Track safe recovery steps for provisioning timeouts or half-created refs without removing unknown resources. Real offline Git tests: byte-for-byte original tracked, staged, unstaged and untracked content plus original HEAD/index; same-description starts get distinct IDs; forced collision injection, stale/swapped admin/path, foreign/primary/adopted checkouts, busy/duplicate start and simultaneous cleanup, failed add/remove, dirty refusal and clean removal retaining branch. Logging: INFO lifecycle transition and ID; DEBUG identity/lease decision codes; WARN drift/failure. (Request §1–2, §6.)
  - Own cleanup authorization and filesystem archival here, not in the CLI. Test incomplete cleanliness observations, ignored files, unknown state artifacts, and restart after archival but before removal finalization.
- [x] Task 3: Add a compatible review gate in `workflow/{state,orchestrator,stages,results}.py` and `workflow/lifecycle.py`, with `tests/workflow/{test_state,test_orchestrator}.py` (independent of 1–2; inject binding callbacks). Extend the existing private record and one atomic plan checkpoint, consume typed `resume` decisions, validate saved plan and binding immediately before unlocking, and ensure approval is durable before any implementation call. Keep existing default auto/library behavior. Test old envelope reads, failure during gate persistence, no planning replay, wrong/stale token, changed plan/policy/HEAD/input, no checks or writes before acceptance, and crash after acceptance but before implementation. Logging: DEBUG safe gate transition; INFO pause/approval; WARN stale binding without token. (Request §4–5, v1-06.)
  - Test that changing task progress does not invalidate the immutable plan digest, but changing any authorized file or dependency does. Use the pre-dispatch and post-plan baseline callbacks even if an injected provider modifies the fixture during planning.
- [x] Task 4: Make resume and cancellation boundary-safe in `workflow/{orchestrator,stages,state,results,lifecycle,tool_loop}.py`, wire the trusted marker into `worktree/registry.py`, and extend `tests/workflow/test_orchestrator.py` and `tests/worktree/test_manager.py` (depends on 2–3). Persist validated continuation, poll cancellation at dispatch/confirmation boundaries, keep checkpoint writer singular, and project terminal `cancelled` without schema changes. Verify pending LLM/check/patch effects win over cancel/resume, committed-unconfirmed patch is never replayed, clean resumes preserve spend and do not repeat completed stages, active cancel stops at the next safe boundary, owner loss leaves intent readable, and terminal/duplicate cancel is idempotent. Logging: INFO requested/consumed/terminal, DEBUG continuation, WARN uncertain effect. (Request §6, v1-06.)
  - Include `workflow/budget.py` and `tests/workflow/test_state.py` for the atomic outcome handoff; keep the existing reservation/settlement math. Inject failures immediately before and after the outcome save for planning, task completion and verification. Before the save, assert no redispatch from an uncertain cursor; after it, assert continuation from the next stage without repeating the saved stage.

### Phase 2 — Composition, CLI, and acceptance

- [x] Task 5: Add `workflow/session.py` as the thin composition/service layer; update `app.py`, `executor/approval.py`, and `metrics/jsonl.py` as needed for explicit root/ledger composition (depends on 2–4). Build `ToolApproval`, exact path/check policy and review fingerprint from admitted worktree files and operator config. Call the existing factory and `run_feature`/`resume`; no stage loop here. Compose check runner and backend without unsandboxed fallback; keep injected provider and offline registry seams. Test with `tests/workflow/test_session.py`: root alignment, stable ledger binding and resumed spend, no CWD change, original config protected, glob limits and no implicit authority, identity/policy drift and no backend refusal. Logging: DEBUG root roles and counts; WARN deny reason, no raw paths/argv. (Request §2, §4–6, ADR-004.)
  - Put execution-config provenance validation at the session boundary; a path resolving into either checkout or Git administration is not an operator config. Pin normalized check arguments and permissions, exclude mapped protected paths from both executor and context admission, and test fingerprint drift without adding source content to planning prompts.
- [x] Task 6: Wire commands in `cli/app.py`, `cli/commands/feature.py`, and `cli/runtime.py` as needed; implement the read-only view in `workflow/session.py` and `worktree/git.py` (depends on 5). Render bounded plan and approval guidance; map busy, stale, pending-effect, unknown, cancelled and terminal outcomes to typed status and next action. Use existing error boundary and preserve bare help/no-config and executable alias behavior. Add `tests/cli/test_feature.py` for commands, modes, exit semantics, terminal status/result, safe cleanup, diff incompleteness and provider/store-write sentinels for help/read-only commands. Logging: DEBUG command/result category; WARN typed rejected operations. (Request §3, §5–7.)
  - Approval guidance must include root `--config` before `resume`. Use `workflow/state.py` for identity-checked archive decoding, and test that config-free inspection/cancellation still works after config removal or corruption and that archive reads reject mismatched identities.
- [x] Task 7: Add offline cross-process acceptance in `tests/cli/test_feature_e2e.py` (depends on 6). Use a real disposable Git repo seeded with a copy of `src/lean_weave/app.py` and the trusted external parser pattern in `tests/workflow/test_feature_e2e.py`; use scripted `MockLLMProvider` calls and actual checkpoint/patch/check adapters. Exercise auto success; review pause, unrelated flag refusal, stale approval and fresh valid approval; cancel idle and during a controlled in-flight effect; fresh-process resume with preserved plan/task counters; terminal retrieval; recoverable edits on failure; duplicate starts/resumes and Git-operation failure; compare origin bytes/index/HEAD. Test both executable entry points, and confirm no LangGraph runtime dependency is added. Run `uv run pytest` offline against the full existing suite. Logging: assert refusal and transition codes, and redaction of deliberately sensitive fixture text. (All acceptance criteria.)
  - Use a synchronization barrier, not a sleep, to request cancellation during the first effect of a multi-call batch. Assert the started effect's durable outcome and usage, no dispatch of the next tool call or provider round, and an unchanged origin. Cover the unconfirmed-effect variant separately.
- [x] Task 8: Document commands and safe operations in `docs/getting-started.md`, a new `docs/worktree-workflow.md`, `docs/architecture.md`, and `.ai-factory/{ARCHITECTURE,DESCRIPTION}.md`; update `README.md` links if needed (depends on 6–7). Cover trusted config/check opt-in, limitations of worktrees versus OS isolation, approval invalidation, safe cancel semantics, failed-run recovery and explicit cleanup/archival, plus ledger/checkpoint locations. Verify every example against help and the offline tests. Logging: document levels and safe diagnostics; do not include actual credential, token, or raw-output examples. (Deliverables, ADR-004.)
