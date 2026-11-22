# Phase 4: Matched workflow benchmark runs

Plan: [index.md](index.md)  
Tasks: 7–8  
Depends on: Phase 2 / Task 4 and Phase 3 / Task 6

## Objective

Run complete workflows against pinned conditions, obtaining independent functional, local-estimate and explicitly authorized live evidence.

## Current-Code Evidence

| Path | Symbols | Why it matters |
| --- | --- | --- |
| `benchmarks/offline.py`, `benchmarks/run.py` | `prepare_case`, `ScriptedTransport`, `success_script`, `run_offline_case`, `validate_live_config` | Existing MockTransport uses real gateway and public workflow composition; current repair and live gates need tightening. |
| `benchmarks/results.py` | `project_metrics`, `compare_workflow_context` | Preserve legacy report compatibility while using Phase 1 analytics for new rows; do not keep a second arithmetic implementation for new results. |
| `tests/cli/test_feature_recovery_e2e.py`, `tests/benchmarks/test_workflow_context.py` | Real fail/repair sequence; paired estimate reduction | Reuse these representative cases and matched gate. |
| `docs/mvp1-readiness.md`, `benchmarks/baselines/v1-offline-baseline.summary.txt` | Live not run; zero-retry historical repair row | Neither is sufficient new evidence. |

## Files to Change

| Path | Action | Required change |
| --- | --- | --- |
| `src/lean_weave/benchmark/{offline,runner,models,live}.py` | Create/move | Installed reusable fixture drivers, strict run schema and explicit live preflight. |
| `benchmarks/{offline,run,results}.py` | Modify | Compatibility delegates or legacy schema readers, not a second runner/collector. |
| `src/lean_weave/{app.py,workflow/session.py,providers/deepseek.py}` | Modify | Trusted injected transport and post-settlement live stop seam, with unchanged production defaults. |
| `tests/benchmarks/test_workflow_matrix.py`, `tests/benchmarks/test_live_limits.py` | Create | Real offline arms and simulated live admission/completion tests. |

## Task 7: Run matched offline workflow arms

### Intent

Exercise real feature start through verification and repair, attributing every incurred LLM attempt without interpreting scripted counters as live measurements.

### Implementation Steps

1. Move reusable offline helpers to installed `benchmark/offline.py`; reuse `ScriptedTransport`, operator config and `prepare_case`. Call the same `FeatureSession.start` used by the CLI. Thread a trusted optional transport through `FeatureSession` and `create_orchestrator` to `create_llm_provider(transport=..., journal=...)`, keeping the actual worktree journal from session provisioning. Use a fixture-only placeholder credential and require a `httpx2.MockTransport` before offline session construction; raise if missing or exhausted, never fall back to a real client. Do not add a YAML/CLI transport selector or monkeypatch global factories. Keep default provenance `offline_scripted`; report the common reservation estimator independently as `local_estimate`.
2. Add `runner.run_matrix(spec) -> BenchmarkRun`; default `v1`/`v2` pairs on the seven pinned manifests. Build each arm in a new private scratch origin with the same pinned SHA, expected verification rules, model/thinking/output and workflow limits, write scope and user task. Validate the input spec before starting and snapshot effective settings (allowlisted scalar knobs and digests only). Record tool/build version, fixture and oracle fingerprints, repetitions, pair IDs, run order, timestamps, expected outcome and variant. Cache disabled by default; expose `v2-map-off` (`context.packing.map_tokens=0`), `v2-tight` (`context.packing.source_tokens=512` against 4096), and `v2-cache-cold` (`context.cache.enabled=true` with an empty private namespace), each compared against default V2 and varying only the named setting. Warm-cache results belong to retrieval, not mutable full workflows; no cross-arm checkout/cache reuse or auto index builds.
3. Grade exact manifest outcomes; actual `verify_then_repair` gets an approved wrong edit, failed first oracle/check, one repair, second verification, and passing final oracle. Insufficient-context and budget-exhaustion require their specific stable refusal/limit reasons and no edits; assert denied reservations do not create phantom sends. Include a valid-plan V2 pack-refusal test. Script may use intended edit recipes, but expected revisions must come from the dispatched prompt or approved tool result, not a hidden fixture read. Preserve origin fingerprint; require no LLM sends for verification/diagnosis or after terminal success.
4. Read Phase 1/2 canonical analytics from the routed state for every executed arm, on success, expected negative and incorrect/partial outcomes; record independent oracle verdict, verification batches, repairs, expected-vs-observed outcome and monotonic arm duration (including failures), with workflow active time separate. Emit one row for every scheduled arm, including not-started/incomplete with reasons. Store sanitized metrics only; never include live stdout, source, tool payloads or raw config.
5. Record total known/covered tokens per gateway attempt and per workflow arm. For per-successful-task reporting expose task-attributed implement+repair usage for verified features and an amortized `all arm tokens / verified successful task count` KPI over all matched scheduled positive arms, including failed attempts and failed features in the numerator; keep planning visible. Null the full KPI on missing eligible evidence or zero denominator, with known sums shown separately. Use the locked `multi_file_feature` pair and its existing strict lower V2 gateway estimate as the representative local gate; no universal costs claim.

### Required Interfaces and Contracts

- `BenchmarkRun` JSON has a schema version, evidence source, validated spec/condition fingerprint, sorted case/arm/replicate rows and fields with coverage; optional timing does not affect deterministic equality checks. Preserve old `python -m benchmarks.run` entry compatibility and historical baseline reading without upgrading their claims.

### Requirement Evidence

Original Request §§2–5, 8–10, 12; v2-06 Phase 4 matched workflow and fail→repair contracts.

### Error Handling and Logging

Abort invalid specs before any arm. Record arm infrastructure errors separately from correctness; DEBUG pair/case/variant/duration and WARN stable error class, no child diagnostics or source.

### Tests

`tests/benchmarks/test_workflow_matrix.py`: both selectors across seven manifests, real repair/retry verification history, task efficiency denominators, unchanged origin, incomplete arm/unknown measurement, extra V2 profiles, repeat fingerprints and global network tripwire independent of pytest fixture state. Verify existing paired estimate assertions.

### Acceptance Criteria

A reproduction of the pinned matrix yields equivalent correctness, accounted use and comparable local-estimate denominators.

### Verification

`uv run pytest tests/benchmarks/test_workflow_matrix.py tests/benchmarks/test_workflow_context.py tests/cli/test_feature_recovery_e2e.py` — passes offline.

## Task 8: Gate live evaluation by explicit limits

### Intent

Permit an operator-authorized DeepSeek fixture evaluation without hidden billable retries or unsupported billing guarantees.

### Implementation Steps

1. Extend `validate_live_config` via installed `benchmark/live.py`. Require `--live`, explicit selected positive case IDs (`bug_fix`, `multi_file_feature` initially), `--live-config-dir` with a case-ID-named external config, `--max-tokens`, `--max-cost-usd`, `--pricing`, `--deadline-s`, and environment-only `DEEPSEEK_API_KEY`. No live attempt for missing or invalid inputs, unsupported fixture/arm, URL override, noncanonical endpoint, unknown price/model, or absent key. Support matched V1/V2 pairs only and cap repeats at 10 for live. No preflight network probes.
2. Prevalidate all arms against the exact strict effective config snapshot to be used. Pin `llm.api_key_env` to `DEEPSEEK_API_KEY`, case write scope and check IDs/argv/executable/timeout/output cap to fixture definitions including data-only check classification; no workspace code. Use private scratch and an immutable invocation copy, isolated per-arm cache and an explicit child environment allowlist; do not inherit proxy, OpenAI/DeepSeek override or TLS override variables. Keep check authority and existing packed/patch guards unchanged. Decode `--pricing` as strict bounded JSON `{version: 1, currency: "USD", as_of: "YYYY-MM-DD", source: string, models: {model_id: {uncached_input_per_million: decimal_string, output_per_million: decimal_string}}}`; reject unsupported models, duplicate keys, non-finite/nonpositive rates or limits. Conservative admission uses uncached input and maximum output rates, fixed decimal arithmetic and upward rounding for any cap estimates, never a cache discount. Do not price reasoning separately unless the provider token semantics explicitly change in later work.
3. Reserve the entire batch allowance before launch: sum the exact per-arm feature-budget caps used by the existing gateway and worst-case costs at the highest USD/token rate for each arm, covering every repetition, including any gateway retry within caps. Refuse above either limit. Use the existing journal's reservations rather than another per-send spend ledger; disable SDK implicit retries, retain gateway admissions, run arms sequentially in killable child process groups with a shared monotonic deadline. On known reported overage, unexpected returned model, missing core usage after an arm, incomplete result, timeout or integrity error, stop further billable work and mark remaining arms not started. Re-read routed state/journal after timeout where safe; do not retry unknown work. Document limits as conservative **admission** controls, not exact remote billing guarantees when provider counts, timeout billing or pricing differ.
   - Add a trusted benchmark-only post-settlement policy callback in `DeepSeekProvider.complete`, invoked after `finalize_attempt` has persisted the outcome and before retry/backoff or returning a response to the tool loop. Wire it from the live child, not from YAML. Stop on missing/inconsistent core counters, unexpected returned model, or charge above the frozen arm cap; read settled charge via the existing journal admission API. Do not change the outcome event, calculate a second charge, or dispatch another tool/model step on a policy stop. Record a stable stop class for the parent to halt the batch. Normal calls without the policy retain current gateway behavior.
4. Report live counters, field coverage, optional cost estimates under the supplied schedule and known provider-cache subsets; no invoiced cost claims. Compare successful comparable pairs with complete coverage separately from all-positive-arm correctness. Keep expected/actual outcomes, missing coverage and deadline/preflight failures explicit. Use existing fixed check execution and safe scratch ownership; never force-remove worktrees with unknown contents or relax cleanup rules.

### Required Interfaces and Contracts

- Live authorization is explicit per invocation and applies only to known fixture code on the canonical provider endpoint, never caller repositories. No sandbox/policy widening, no model-based oracle. `--live` cannot run retrieval or local-estimate benchmarks as if they were provider measurements; estimates in live results remain separately labeled.

### Requirement Evidence

Original Request §§8–12 and acceptance 3–5; existing `benchmarks/run.py:validate_live_config`, rules on network opt-in and credentials.

### Error Handling and Logging

Fail closed before any launch when settings are invalid. On runtime shortfall, keep partial results, charge unknown conservatively, and return stable nonzero status. Log counts, caps and reason codes; no key, raw pricing file/config, absolute scratch path or command payload.

### Tests

`tests/benchmarks/test_live_limits.py`: child-launch tripwire for missing opt-in/key/limits, malformed quotes or rate, mismatched endpoint/key variable/check/limit, unsupported model/case, token and cost oversubscription counting both arms/repeats, contaminated parent environment, deadline cleanup, nonzero child and false success. Use scripted settlement with incomplete usage on a retryable reply and a later tool-loop round; assert the original outcome is durable and no next send is admitted. No paid calls in tests.

### Acceptance Criteria

Live work is impossible without explicit budgets and opt-in; all recorded evidence remains truthful on timeout, failure or partial usage.

### Verification

`uv run pytest tests/benchmarks/test_live_limits.py tests/benchmarks/test_run.py` — all tests pass with no live requests.

## Phase Risks and Mitigations

- The checked-in V1 7/7 result did not exercise repair; certify with the explicit sequence rather than republishing historical success as new evidence.
- Model outputs, remote cache, and vendor latency are uncontrolled; preserve run conditions and disclose them rather than promising repeatability of live tokens.

## Phase Completion Checklist

- Both tasks meet acceptance and offline verification passes.
- Mark Tasks 7–8 in `index.md` after verification.
