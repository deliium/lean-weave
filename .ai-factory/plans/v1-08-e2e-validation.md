# Implementation Plan: MVP 1 End-to-End Validation and Token Baseline

- Plan ID: `v1-08-e2e-validation`
- Branch: none created (plan-only change on `main`)
- Created: 2026-11-21

## Original Request

Plan ID: v1-08-e2e-validation
Plan Title: MVP 1 End-to-End Validation and Token Baseline
Milestone: MVP 1 - Core Coding Pipeline

Dependencies:
- v1-01-architecture
- v1-02-project-foundation
- v1-03-deepseek-gateway
- v1-04-context-builder
- v1-05-code-tools
- v1-06-orchestrator
- v1-07-worktree-state

Goal:
Validate the complete Lean Weave MVP 1 feature development pipeline and establish a reproducible baseline for future token efficiency improvements.

Architecture:
- Minimal deterministic Python state machine.
- Existing WorkflowOrchestrator.
- Existing StateStore and checkpoints.
- DeepSeek API.
- Minimal V1 Context Builder.
- Approved code editing tools.
- Git worktree isolation.
- Public CLI executable: wl.

ADR-002 and RULES.md are authoritative.

Before planning:
- Inspect completed V1 implementation.
- Identify existing tests and benchmark infrastructure.
- Reuse existing fixtures and MetricsCollector.
- Do not redesign the orchestrator.
- Do not duplicate previous testing infrastructure.

Functional requirements:

1. Complete workflow validation

Test:
- Plan generation
- Plan validation
- Task-by-task implementation
- Code modification
- Verification
- Failure diagnosis
- Bounded retries
- Successful completion
- Explicit failure
- Budget exhaustion
- Interrupted execution and resume

2. CLI integration

Validate:
- wl init
- wl doctor
- wl feature --auto
- wl feature --review
- wl status
- wl resume
- wl cancel
- wl result

Verify correct exit codes and structured results.

3. Git isolation

Test:
- Clean repositories
- Dirty original working trees
- Isolated feature worktrees
- Conflicting feature execution
- Failed task recovery
- Preservation of user changes
- No unintended merges or commits

4. Workflow reliability

Verify:
- Deterministic state transitions
- Terminal state behavior
- Correct retry limits
- No repeated successful stages
- Safe checkpoint recovery
- Correct approval propagation
- Stale workspace rejection
- No repeated uncertain side effects
- No unauthorized operations

5. Token accounting

Measure:
- Input tokens
- Output tokens
- Total processed tokens
- Cache-hit tokens when available
- Reasoning tokens when available
- LLM call count
- Retry count
- Tokens per workflow stage
- Feature-level cumulative usage

Preserve the original provider usage semantics.

Do not double-count cached or reasoning tokens
when they are already included in reported totals.

6. Benchmark dataset

Create small reproducible Python fixture repositories.

Include:
- Simple bug fix
- Single-file feature
- Multi-file feature
- Test repair
- Failed verification and retry
- Context-insufficient task
- Token budget exhaustion

Each task must define:
- Initial Git revision
- User request
- Allowed files and operations
- Expected behavior
- Verification commands
- Success conditions

7. Baseline measurements

Record V1 performance:
- Total processed tokens
- LLM calls
- Context size
- Retry counts
- Test success rate
- Execution duration

Persist machine-readable results.

Provide a human-readable benchmark summary.

Distinguish:
- Mock-based functional tests
- Locally estimated token counts
- Actual DeepSeek API measurements

Live API benchmarks must be explicitly enabled
and remain within configured budgets.

8. Comparison methodology

Define a reusable protocol for V2:

- Same initial repository revisions
- Same user requests
- Comparable model settings
- Same verification conditions
- Same execution limits where applicable
- Full accounting for failed attempts

Do not claim savings without measured results.

9. Documentation

Document:
- Installation
- Configuration
- API credentials
- CLI usage
- Automatic and review modes
- Worktree behavior
- Resume and cancellation
- Security restrictions
- Token metrics
- Known MVP limitations

Constraints:
- No LangGraph runtime integration.
- No new orchestration framework.
- No MCP or ACP.
- No background worker.
- No embeddings or vector database.
- No advanced repository indexing.
- No unrelated feature development.

Acceptance criteria:
1. Complete feature execution works through wl.
2. Offline end-to-end tests pass.
3. Workflow recovery is verified.
4. Safety restrictions are verified.
5. Token accounting is consistent.
6. Benchmark fixtures are reproducible.
7. V1 baseline results or clearly identified measurement gaps are preserved.
8. Documentation is complete.
9. MVP 1 readiness is documented.

Deliverables:
- E2E test suite
- Benchmark fixtures
- Baseline measurement format
- Benchmark reporting
- Updated documentation
- MVP 1 readiness report

Preserve the existing architecture and prioritize
correctness, reproducibility, and measurement.

Save using the exact plan ID:
v1-08-e2e-validation

## Settings

- Testing: yes; network blocked by default.
- Logging: verbose; `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`); bounded structured counts, transitions and reason codes only.
- Docs: yes; mandatory `/aif-docs` checkpoint.
- Dependencies: [v1-01-architecture](v1-01-architecture.md), [v1-02-project-foundation](v1-02-project-foundation.md), [v1-03-deepseek-gateway](v1-03-deepseek-gateway.md), [v1-04-context-builder](v1-04-context-builder.md), [v1-05-code-tools](v1-05-code-tools.md), [v1-06-orchestrator](v1-06-orchestrator.md), [v1-07-worktree-state](v1-07-worktree-state.md).

## Roadmap Linkage

Milestone: "End-to-end MVP feature loop"

Rationale: This is the roadmap item corresponding to end-to-end execution and a token baseline under the requested **MVP 1 - Core Coding Pipeline** milestone. The separate hardening and LangGraph items are not closed by this plan.

## Requirements Reconciliation

Authority: [ADR-002](../adr/ADR-002-orchestrator-langgraph-vs-minimal.md) and [RULES.md](../RULES.md) govern this plan. [ADR-004](../adr/ADR-004-context-isolation-and-state.md) and the delivered v1-05–v1-07 contracts refine approval, recovery, and isolation. The v1-01 acceptance reference covers a historical architecture gate, not today's public CLI. The roadmap identifies scope, not behavior. Older “help-only,” “not wired,” and “max_retries reserved” documentation is stale where the implementation contradicts it; reconcile the user docs against tested behavior, not the other way around.

| Combination | Accepted input, persisted state, output and side effects | Source / verification |
|---|---|---|
| `init` / `doctor`, no provider or key | Use product config defaults or an explicit path; `init` writes only the config and refuses overwrite by default. `doctor` is diagnostic only; missing key is a warning, not permission to call the network. | Request §2; `docs/cli.md`; public executable smoke test using generated default config. |
| Auto + valid external config + approved paths/checks | From pinned local `HEAD`, persist routing and a validated plan; execute ordered tasks in the target worktree, then deterministic checks. Invalid plan or unavailable authority fails before source effects. | Request §§1–4; v1-05–v1-07; assert stage/task order, stdout projection, final bytes and no writes before plan validation. |
| Review + absent, incorrect, stale or valid approval | Persist the validated plan at the gate before edits. Missing/incorrect token or changed policy, baseline, or workspace cannot dispatch; matching token on unchanged binding continues without a second plan call. | Request §§2, 4; v1-07 “CLI and authority”; assert call counts, worktree bytes and persisted gate behavior. |
| Verification success, attributable failure, non-repairable failure, or budget exhaustion | Keep verification local. Only recognized diagnostics attributed to an eligible file can trigger repair within the retry limits; generic failures terminate without repair. Persist settled usage and task/retry counters; terminal inspection/resume does not redispatch. | Request §§1, 4–5; ADR-002; v1-06; `verify/runner.py::repair_attribution`; assert final reason, check/call counts and no new dispatch after budget rejection. |
| Safe interruption, uncertain effect, or cancellation | Resume from a proven durable continuation with frozen approvals and cumulative spend. Block replay on uncertainty, preserve dirty edits and effect markers; cooperative cancellation does not roll back. Exercise idle and active paths. | Request §§1, 3–4; v1-07 “Control and projection”; assert restart behavior, exact effect counts, and persisted outcome. |
| Clean/dirty origin, concurrent lease conflict, or stale worktree | Use real linked worktrees for execution; a dirty origin stays byte-for-byte intact and is not copied. Reject competing mutation for the same target and stale identity without implicitly adopting/recreating the checkout. | Request §§3–4; ADR-004; v1-07 “Ownership and location”; compare origin HEAD, branch, index, and tracked/untracked contents. |
| Data-only public checks vs. workspace-code checks | Public acceptance uses a trusted external executable with fixed, data-only argv. Workspace-code checks are rejected without a trusted injected isolation backend. An injected test double is not a production sandbox. | Request §4; RULES; v1-05 and v1-07; test positive data-only path and fail-closed workspace-code preflight. |
| Mock functional, local estimate, or opt-in DeepSeek | Store separate provenance; report unavailable fields as unavailable, not zero. Count cached/reasoning details as subsets and include failed/uncertain attempts without inventing provider usage. Live execution additionally requires the DeepSeek endpoint, exact fixture approvals and valid budgets. | Request §§5, 7–8; v1-03; cross-check journal, workflow result and benchmark report; assert rejected live preflight makes zero sends. |

### Evidence and scope boundaries

- Reuse the existing Typer application and console entry point, workflow state/result schemas, `JsonCheckpointStore`, `ToolApproval`, `ContextProvider`, `VerificationRunner`, and `MetricsCollector`. The benchmark is developer tooling, not another orchestrator or new `wl` product command.
- Existing `tests/cli/test_feature_e2e.py` covers command flow and Git without an actual edit; `tests/workflow/test_feature_e2e.py` covers actual patches, repair and checkpoints outside the public CLI; `tests/providers/test_deepseek_crosslayer.py` covers real gateway accounting with mocked HTTP. `wl llm check` and `spikes/deepseek_probe/` are historical/smoke infrastructure, not a feature baseline. Do not reuse the spike's key-presence live behavior.
- Inspection found that `workflow/session.py` and `workflow/lifecycle.py` bind approved paths and check IDs, but do not fully bind check definitions or limits. Test same-ID check drift and limit drift separately from path/ID drift, plus the planning-window fingerprint and identity revalidation at execution boundaries. These are v1-07 contract gaps to reproduce and fix locally if confirmed, or explicitly mark as readiness blockers; do not document full drift protection from narrower tests. Concurrent external Git/editor mutations are unsupported; do not claim a race-free OS sandbox.
- Public no-injection success uses external, fixed-argv checks that inspect fixture data rather than import or execute fixture modules. A named test-repair fixture can verify repaired test source structurally on that path; separately verify its behavioral expectation with a trusted test isolation seam if available. Label each evidence tier; never misclassify a test runner as data-only.
- Limit any product changes to observed acceptance defects or missing numeric telemetry at existing seams. Reproduce first; do not weaken path admission, approval or checkpoint guards. Do not add production mock flags, a direct-execution fallback, or a new runtime dependency. File issues or record limitations for out-of-scope defects.
- Public CLI reports must use the current typed projection and serialization; don't add a second result format for product commands. Benchmark results are separate versioned evidence.

## Commit Plan

- After tasks 1–4: `test: validate the MVP workflow through wl and isolated worktrees`
- After tasks 5–8: `test: capture reproducible MVP token baseline evidence`

These are suggested developer checkpoints, not authorization to commit or push.

## Tasks

### Phase 1 — Offline acceptance and recovery

- [x] Task 1: Build the shared seven-case fixture corpus. Add `benchmarks/fixtures/` (manifests and source trees) and `benchmarks/fixtures.py` (materializer), reused by the CLI tests and baseline runner. Cover a simple bug fix, single-file feature, ordered multi-file feature with a dependency, test repair, failed verification then repair, insufficient context, and token budget exhaustion. Each manifest pins the expected Git `HEAD`, user request, exact existing write paths, allowed operations and check IDs, trusted check argv/template, expected behavior (including deliberate negative outcomes), and pass criteria. Materialize from local files into a fresh temporary Git repo using fixed Git identity, timestamps, file modes and line endings, isolated configuration and no remotes; check the actual revision against the manifest. Keep expected outputs and check drivers outside the materialized repo/model context. Use fixed-argv fixture-data checks that fail on the initial revision for positive cases and pass only on the intended edit; verify that a test-repair oracle distinguishes a repaired test from a deleted or weakened one. Prove reproducibility on two fresh materializations, with the same revision, status and oracle output. Do not run Git initialization against the developer checkout. **Logging:** DEBUG manifest ID/revision and check IDs; ERROR fixture mismatch reason only. **Evidence:** Request §§3, 6.

- [x] Task 2: Join public CLI, actual edits, deterministic verification and production accounting in one offline path (depends on 1). Extend `tests/cli/test_feature_e2e.py` and reuse the transport pattern from `tests/providers/test_deepseek_crosslayer.py`; inject `httpx2.MockTransport` at the gateway HTTP seam, not a fake session or orchestrator, so reservations and settlements go through the real per-worktree journal. Drive a successful `--auto` run with revision-checked edits and an observable check; drive `--review` through a persisted plan to explicit approval, with no patch/check before approval. Cover plan rejection (including out-of-policy file and bad task dependency) and ordered multi-file tasks with one verified outcome per task. Check `init` and `doctor` with the real installed `wl` executable offline, using the representative config emitted by `init` (reuse existing command tests for edge assertions), then use the same registered public Typer app for injected offline feature runs. Use distinct trusted external operator config for execution and prove generated repository defaults alone cannot authorize it. Check typed `FeatureView`/`WorkflowResult` and stable CLI `key=value` fields (`feature_id`, `status`, `stage`, `reason`, `verification`, `usage`, `next_action` as applicable); current commands are not a JSON API. Assert successful command/status exit `0` including rendered failure or budget outcomes, typed operational errors `1`, parser misuse `2`, `ConfigError` `3` (including invalid mode combinations), and no dependencies created by read-only queries. Reopen the journal after the run and correlate usage without treating synthetic provider-shaped values as live. **Logging:** DEBUG stage/check IDs and counts, INFO terminal/review transitions, WARN safe denial codes, no payloads. **Evidence:** Request §§1–2, 4–5; auto, review and config rows.

  Put the reusable offline driver and scripted transport in `benchmarks/offline.py` for both this suite and Task 6; keep pytest wiring in tests. Use `app.py::create_llm_provider` at the lookup in `app.py::create_orchestrator`, preserving its caller-supplied accounting instance. Assert object identity for the journal used by the provider and workflow budget, then verify its contents after reopening. Plain `MockLLMProvider` does not populate the session's journal. From a fresh process invoke the installed `wl status`/`wl result` without config or key and assert the saved outcome and unchanged checkpoint/journal content; routing setup can touch directory metadata and is not a zero-filesystem-write guarantee. Add a no-network executable smoke of the feature command's preflight; reserve a full uninjected executable success for the opt-in live case.

- [x] Task 3: Verify retries, budget stops and recovery across command invocations (depends on 2). Extend `tests/cli/test_feature_e2e.py` and reuse the deterministic fault seams and fixtures in `tests/workflow/test_feature_e2e.py` and sibling lifecycle/state suites. Cover bounded failure diagnosis passed to `RETRY`, successful repair, retry limit zero and exhausted retries, low token limit before dispatch and across resume, failure and cancellation terminal projections, and idempotent terminal resume. Reuse existing stage-level tests for PLAN/IMPLEMENT/VERIFY/RETRY; add cross-layer evidence for a fresh session resuming from a proven checkpoint with completed work and spend retained. Pair it with an unresolved effect or crash after a durable patch but before a committed task outcome: repeated resume must report inspection required, preserve markers/edits, and issue no repeat send, write, or check. Drive idle cancel and active cooperative cancel using explicit synchronization, not sleeps; verify the persisted request and no new effect once honored. Include context-insufficient outcome with no widened access; assert typed failure/stop supported by the existing workflow, not a new state. **Logging:** DEBUG stage/attempt counts, INFO cancel and recovery transitions, WARN budget or manual-inspection reasons; no raw diagnostics. **Evidence:** Request §§1–5; retry and recovery rows.

  Where process death is required, isolate it to a test child in the disposable fixture with a deterministic fault boundary and fresh re-entry; distinguish `interrupted/unresolved_effect` from `failed/manual_inspection` after an applied-but-uncommitted effect. Test that explicit cleanup refuses an edited worktree without removing files or branches; reuse existing clean-terminal archival coverage.

  For Task 1's repair fixture, reuse the external AST checker pattern in `tests/workflow/test_feature_e2e.py`. Have the first edit fail with a bounded recognized diagnostic (e.g. `E999`) on one approved relative path, then require the repair to satisfy the semantic oracle. Separately assert that a generic nonzero check without repair attribution does not invoke `RETRY`; do not treat every failure as retryable.

- [x] Task 4: Verify Git isolation and frozen approvals in the integrated flow (depends on 2; combine setup with 3 where useful). Extend `tests/cli/test_feature_e2e.py` and reuse `tests/worktree/` real-Git fixtures and leases. Compare source `HEAD`, branch, index and contents before/after clean and dirty-origin runs, including staged, unstaged and untracked data; show only pinned content reaches the target. Prove separate managed runs do not share source writes or checkpoints; competing execution for the same target is rejected or handled only via the documented cancel path. Reject stale workspace identity/HEAD, stale or wrong review token, operator-config/policy drift, unauthorized write/check, and a workspace-code check with no injected `IsolationBackend`. Confirm failed task/recovery edits are confined to the target, remain available for inspection, and no commits, merges, pushes or origin cleanup occur. Reuse the existing tool policy tests for detailed denials rather than duplicating them. **Logging:** DEBUG lease and approval decision categories, WARN stable rejection reasons; no approval token, raw paths from denied inputs or Git stderr. **Evidence:** Request §§2–4; review, concurrency and data-only rows.

  Add CLI regressions for both review approval and safe continuation with changed same-ID check definitions, effective execution limits, and `workflow.state_dir`. On nonterminal resume, reject drift before any provider dispatch, patch, or check; changing the state directory must not open a new usage ledger or reset spend. Exercise the start-time fingerprint with a deterministic mid-plan edit. Reuse existing bindings in `workflow/session.py` and `workflow/lifecycle.py` for scoped fixes if needed. If a case cannot be made safe within this plan, record a readiness blocker, not a passing result based on an expected failure.

### Phase 2 — Measurement, comparison and readiness

- [x] Task 5: Implement a versioned metric projection over existing sources (depends on 2–3). Add `benchmarks/results.py` and tests under `tests/benchmarks/`; use `JsonlMetricsCollector.summary(feature_id)` plus its bounded attempt projection and the reopened checkpoint/result. If the attempt projection lacks the reservation input estimate, extend the existing read-only metrics projection minimally and compatibly rather than parsing another ledger or changing settlement. Report per-attempt, per-stage and feature rollups for known prompt/input, completion/output and total processed tokens, with cache-hit/reasoning as optional subsets; do not add details back to processed totals or treat missing as zero. Include field coverage and partial-usage provenance, estimated input and estimated/budget charges separately, logical workflow calls, journal reservations/confirmed outcomes/uncertain attempts separately, workflow repair count separate from gateway retry attempts, and zero verification LLM sends. Define context size as admitted context/prompt dimensions available without storing payloads: label actual numeric units and boundaries separately from full prepared-request token estimates (tools/metadata included there). If unavailable, report a gap or add bounded numeric telemetry at the existing context seam. Include failed/unresolved attempts; do not count a denied reservation as a send or an unresolved reservation as a confirmed send. Test consistent, incomplete, absent, and inconsistent usage, nonzero detail fields, negative outcomes and restart without double-counting, using existing usage fixtures and metrics tests. **Logging:** DEBUG coverage and projection counts; WARN missing/inconsistent aggregate reasons, never raw journal rows or prompts. **Evidence:** Request §§5, 7–8; v1-03 accounting.

  Use per-attempt denominators (journal presence flags indicate at least one reported value, not complete coverage). Preserve the gateway’s conservative reservation and settlement rules; the CLI's prompt-plus-completion display is a checkpoint projection, not the attempt ledger. The workflow currently sets gateway `retry_limit=0`; do not interpret calls at stage `retry` as transport retries.

  Read workflow retries from checkpoint `FeatureState.retry_count`, not `WorkflowResult`. For context size, observe `ContextPack.estimated_tokens` and bounded admitted excerpt sizes at `context/provider.py::DeterministicContextProvider.build`; label these as initial stage bundle metrics, distinct from full per-attempt prepared input estimates. For verification pass rates, count actual check executions and outcomes at the approved check seam (including failed repair attempts; exclude checks that never ran). A final `VerificationResult` is one batch, not history. If a live run has no reliable context or check-history observation, export those fields as unavailable rather than deriving them from final status or retry count.

- [x] Task 6: Add the offline runner, explicit live path, and machine/human reporting (depends on 1–2, 4–5). Add `benchmarks/run.py`, reporting support in `benchmarks/results.py`, and runner tests under `tests/benchmarks/`. Default to mock functional evidence and separately labeled local estimates; allow live only via an explicit `--live` request, environment-only credentials, a trusted external config, and an explicit bounded case/repetition selection. Never enable live from key presence or normal pytest. Invoke the installed `wl` entrypoint for the live feature loop with no HTTP substitution; use the approved data-only fixture oracle, the real DeepSeek gateway, existing call/token/output/timeout caps, an overall finite deadline, and serial bounded runs with a configured aggregate ceiling enforced through conservative per-run allowance. Do not automatically retry a crashed or ambiguous run. Reuse Task 2's offline driver, labeled as in-process CLI evidence; do not rely on the pytest socket guard for a subprocess offline guarantee. Persist a versioned JSON artifact even for failures/interruptions or unavailable live data, with revision, config/settings fingerprints and safe comparable fields (model, thinking, caps and check descriptors), timing via monotonic clock, exit/status, retry counts, check passes/attempts, case/oracle successes/attempts, and completeness flags. Render a human-readable summary from the same data; distinguish expected negative cases from unexpected failures, and give denominators for check success rates and case correctness. Persist only bounded counts and allowlisted metadata, never raw prompt, response, output, token, config, or environment values containing secrets. **Logging:** INFO case ID/mode/outcome and aggregate counts, WARN skipped/incomplete gates, ERROR safe runner failures. **Evidence:** Request §§5–8.

  Reject live selections/configurations that exceed the aggregate ceiling before launching; for partial runs retain unresolved reservations or report unknown spend and stop the batch. Caps govern admission, not a guarantee of billed usage if the provider disagrees with a reservation. Report overages and stop instead of discarding them. Run only sanitized local fixtures; an operator cannot point the benchmark at an arbitrary developer checkout.

  Validate the effective live config before launching `wl`: require the provider's canonical `https://api.deepseek.com` endpoint and match the fixture's approved paths, check IDs, exact trusted executable/argv and data-only classification. Use a private external config snapshot of the validated policy for the child, without credentials in the file. Negative runner tests must show that custom endpoints, wider approvals and altered checks never launch a billable run.

- [x] Task 7: Document the supported workflow and matched V2 protocol (depends on 2–6). Update `README.md`, `docs/getting-started.md`, `docs/cli.md`, and `docs/worktree-workflow.md`; add `docs/benchmarks.md`. Document installation, exact CLI modes and exit/result semantics, explicit trusted operator config, env-only keys, worktree isolation vs. execution sandbox, safe continue vs. manual inspection, cooperative cancellation, and known limits including the lack of a public workspace-code backend. Correct stale provider, retry and CLI descriptions found during validation. Document the metric units, attempt/coverage semantics and redaction, and reproducible commands for offline and explicitly enabled live measurement. Define V2 paired runs with identical fixture revisions/requests, equivalent provider model/settings, matched check commands/oracles and limits, comparable run counts/environment, and all failed/uncertain attempts retained. Keep mock, estimates, and actual provider results separate; disclose missing coverage and cache/time conditions, and allow savings claims only against measured comparable results with correctness reported. **Logging:** no new runtime logging; examples show redacted INFO/WARN and opt-in DEBUG. **Evidence:** Request §§7–9; RULES and documented worktree contract.

- [x] Task 8: Capture V1 baseline evidence and publish the MVP readiness decision (depends on 1–7). Persist sanitized reproducible offline baseline JSON under `benchmarks/baselines/`, with human summary and readiness criteria in `docs/mvp1-readiness.md`; record run commands, revisions, modes, versions, dataset outcomes and evidence links. Run focused test groups and the full offline suite (`uv run --locked pytest`); do not imply tests ran at plan time. A successful explicitly authorized DeepSeek run with a correct public CLI edit and journal evidence is distinct from offline readiness; if not run or incomplete, persist the reason and coverage gap without fabricated numbers or closing the real-run roadmap gate. Record what works, outstanding defects/limits, and explicit readiness verdict against all nine criteria; leave hardening/deferred orchestration untouched. **Logging:** no new runtime logging; evidence retains bounded counts, safe reasons and environment controls only. **Evidence:** Request §§7–9 and acceptance criteria; roadmap “End-to-end MVP feature loop.”

## Acceptance Criteria Mapping

For Tasks 7–8, document and report three separate verdicts: offline CLI acceptance, live end-to-end evidence, and approval/recovery safety. Use `pass`, `fail`, or `not run` with evidence and reasons. A missing live run leaves the real-run roadmap item open; it does not invalidate passing offline evidence. Any confirmed unresolved approval or recovery gap blocks a pass for safety and must be reflected in the overall MVP readiness verdict.

| Criteria | Required evidence |
|---|---|
| 1–2: `wl` feature execution and offline suite | Tasks 1–2, 8: actual fixture edit plus oracle through the public command surface, entrypoint smoke, and passing offline command/test artifacts. |
| 3–4: Recovery and safety | Tasks 3–4, 8: safe resume, no replay on uncertainty, terminal behavior, no dispatch after effective-authority or state-dir drift, stale rejection and preserved origin; unresolved failures block the safety verdict. |
| 5: Accounting | Tasks 2, 5–6: one shared production journal reopened after execution, coverage-labelled rollups, no subset double count or conflation of attempts with sends. |
| 6: Reproducibility | Tasks 1, 6–7: pinned fixture revisions, matched inputs/checks and repeatable runner protocol. |
| 7: Baseline or gaps | Tasks 5–6, 8: versioned JSON and human summary with explicit live provenance or missing measurements. |
| 8–9: Docs and readiness | Tasks 7–8: updated guides and explicit readiness decision with evidence and limitations. |
