# Implementation Plan: DeepSeek API Gateway and Token Metrics

Plan ID: v1-03-deepseek-gateway  
Branch: none (exact plan identifier requested; no branch or worktree created)  
Created: 2026-11-18

## Original Request

Plan ID: v1-03-deepseek-gateway
Plan Title: DeepSeek API Gateway and Token Metrics

Dependencies:
- v1-01-architecture
- v1-02-project-foundation

Goal:
Implement a centralized DeepSeek API gateway with accurate token accounting and strict execution budgets.

Requirements:
1. Implement DeepSeekProvider using the approved API client.
2. Support configurable models, timeouts, retries, and output limits.
3. Support tool calling and structured outputs.
4. Support thinking configuration when available.
5. Preserve exact usage information returned by the provider.
6. Track:
   - input tokens
   - output tokens
   - total tokens
   - cache-hit tokens when available
   - reasoning tokens when available
   - model name
   - workflow stage
   - request duration
   - retries
7. Implement cumulative token budgets per feature.
8. Distinguish measured API usage from locally estimated tokens.
9. Implement MetricsCollector with JSONL persistence.
10. Implement MockLLMProvider for deterministic testing.
11. Handle API errors, network failures, rate limits, and invalid responses.
12. Provide a minimal CLI command to test the configured connection.
13. Add tests for token aggregation, retry accounting, and budget exhaustion.

Token-efficiency requirements:
- Never send full conversation history by default.
- Make every request's context explicit.
- Avoid unnecessary system instructions.
- Support fresh, independent requests for separate tasks.
- Prevent unbounded retry loops.
- Count failed attempts whenever usage data is available.
- Track cached tokens without incorrectly treating them as zero processed tokens.

Constraints:
- Do not implement a complete coding agent.
- Do not implement the LangGraph workflow.
- Do not build a generic multi-provider framework beyond existing interfaces.
- No external API calls in automated unit tests.

Acceptance criteria:
- A real DeepSeek request can be executed.
- Usage information is recorded and aggregated.
- Budget exhaustion stops additional requests.
- Mock tests pass without network access.
- Gateway behavior is independently testable.

Save the plan using the exact identifier:
v1-03-deepseek-gateway

## Settings

- Testing: yes
- Logging: verbose (safe metadata only; honor `LEAN_WEAVE_LOG_LEVEL` and `LOG_LEVEL`)
- Docs: yes (mandatory `/aif-docs` checkpoint)
- Dependencies: [v1-01-architecture](v1-01-architecture.md) and [v1-02-project-foundation](v1-02-project-foundation.md); both completed.

## Roadmap Linkage

Milestone: "LLMProvider (DeepSeek)"  
Rationale: This plan delivers the DeepSeek integration and usage counters that support it, but does not complete the separate Core StateStore + MetricsCollector milestone.

## Requirements Reconciliation

Authority: The original request is the feature scope. `RULES.md` and accepted ADRs govern architecture and safety; v1-01 references document the wire contract, and v1-02 code defines the compatibility baseline. ADR-003 selects the runtime client; v1-02's temporary exclusion of HTTP/LLM SDKs and the current dependency-policy test conflict with adding that client now. Narrow the runtime exception to `openai` for this gateway; keep every unrelated MVP exclusion. Do not treat the historical architecture spike or its fixtures as evidence that the live API has passed.

| Decision / supported combination | Source and relationship | Verification evidence |
|---|---|---|
| Fresh plain request, with thinking off by default or explicitly enabled on a supported model: require caller-supplied messages, feature ID, and stage; send no implicit transcript or system text. Return only the resulting message, usage, and finish metadata. Persist accounting only. | Original Request 1–4 and token-efficiency requirements; `ADR-003-llm-provider-deepseek.md` — Decision; `ADR-004-context-isolation-and-state.md`; `RULES.md`. | Capture successive SDK requests and prove no message carryover. Validate overrides and thinking options before transport. |
| Tool request with thinking off: return calls without executing them; `content: null` is valid with valid tool calls. With thinking on, a supplied replay must include prior assistant `reasoning_content`, tool calls, and matching result IDs. Reject malformed replay locally; reasoning text stays in flight. | `v1-01-deepseek-validation.md` — Capability matrix / API notes refine Original Request 3–4. | Existing spike fixture shapes through the production adapter; captured second request verifies reasoning echo; journal/logs contain neither reasoning nor tool payloads. |
| JSON object with thinking off or on: send `json_object` plus caller-provided JSON instruction/example; parse locally and optionally validate against a caller-supplied Pydantic model. Treat blank, invalid, non-object, schema-invalid, or truncated output as invalid completion. Tools and JSON object mode are separate MVP modes; reject requests asking for both rather than silently choosing one. No `/beta` schemas. | `ADR-003` — Decision; `v1-01-deepseek-validation.md` — Capability matrix and API notes. Explicit narrow MVP interpretation of Original Request 3. | Offline valid/blank/non-object/schema/truncation tests; unsupported configurations make no call. |
| Measured usage on success **or failure**: persist the provider usage separately from normalized counters, with requested/returned model, stage, duration, status, and retry ordinal. Cache and reasoning are subsets, not additions or deductions. If usage is absent or incomplete, mark unknown fields and separately charge a conservative local estimate; never replace or label estimates as API usage. | Original Request 5–9, 11, 13; `TokenUsage` and `MetricsCollector` contracts; `ADR-003` — Consequences. | Fixtures for optional/missing fields, explicit zero, bad output with usage, error-body usage, and mixed feature/stage/model totals after reopen. |
| Budget admission applies to every outbound attempt, including retries; a denied reservation sends nothing. Recover prior spend from JSONL; unclosed reservations remain charged and are reported as uncertain, not as confirmed sends. Admission is atomic across collectors for one journal; unsupported competing process writers fail closed. | Original Request 7–9, 13 and token-efficiency requirements; strict local enforcement refines `v1-01-token-budget-principles.md` (the earlier prompt soft cap is not a gateway budget). | Boundary, retry denial, partial usage, crash/reopen, competing collectors, and feature isolation tests with exact transport counts. |
| Only the explicit `wl llm check` command dials DeepSeek, with a small, bounded request and an isolated `probe` attribution; ordinary commands and help remain offline. A legacy injected collector that cannot reserve atomically must not silently bypass the budget. | Original Request 7, 9, 12; v1-02 `AppContext`, `cli/runtime.py`, `cli/app.py`, and `docs/cli.md`. | `CliRunner` exercises registered command, injected transport, generated product YAML, and incompatible journal rejection; separate opt-in manual live smoke. |

## Commit Plan

- **Commit 1** (after tasks 1–3): `feat: add DeepSeek gateway configuration and usage ledger`
- **Commit 2** (after tasks 4–6): `feat: enforce DeepSeek request budgets and account for retries`
- **Commit 3** (after tasks 7–9): `feat: add DeepSeek connection check and offline verification`

## Tasks

### Phase 1: Configuration and accounting foundation

- [x] Task 1: Activate the approved client and validate gateway settings.
  - Promote the synchronous OpenAI SDK from spike-only to a bounded runtime dependency in `pyproject.toml` and `uv.lock`, keeping spike resolution compatible. Update `tests/test_dependency_policy.py` to enforce the narrow exception without permitting unrelated SDKs or frameworks.
  - Extend `src/lean_weave/config/models.py` and `config/loader.py` (including the init template and safe config output). Preserve `llm.model`, `llm.base_url`, `llm.api_key_env`, `llm.timeout_s`, and `llm.thinking`. Add strict, finite defaults: `llm.max_retries: 2` (extra attempts, range 0–5), `llm.max_output_tokens: 1024` (positive), and `workflow.feature_token_budget: 20000` (positive, total processed tokens). Leave `workflow.max_retries` exclusively for future workflow retries. Allow explicit request model/thinking/output overrides; the output override may lower but not raise the configured ceiling. Validate model capability and output bounds in the adapter; preserve an environment-only key and existing YAML fallback behavior.
  - Validate `llm.base_url` before model dump or SDK construction: absolute HTTPS, no userinfo, query, or fragment. Reject a URL containing the configured key at the authenticated-use boundary without echoing it; ensure the direct YAML/JSON output in `cli/commands/config.py` does not print known secret values embedded in configuration strings. Keep the operator-configured host/path supported. Verify both output formats and validation diagnostics separately from log capture, following `.ai-factory/patches/2026-11-16-22.13.md`.
  - **Evidence:** Reconciliation rows 1 and 6; `tests/config/` covers defaults, existing config, init round-trip, and invalid limits. **Logging:** DEBUG effective settings and source through existing redaction; never log key values, headers, or config payloads.

- [x] Task 2: Define the typed attempt, usage, and error boundary (depends on 1).
  - Extend `src/lean_weave/contracts/_types.py` additively for feature ID, request output cap, optional local Pydantic result validator, reported usage, and parsed structured result. Keep `LLMProvider.complete(request)` synchronous. Require a non-empty caller feature ID, a valid stage, and explicit messages at the gateway entry. Use a gateway attribution type extending `StageName` with `probe`; confine probes to a reserved feature ID that normal stages cannot use, without changing `StageName` or `FeatureState`. Preserve `TokenUsage` and the existing `MetricsCollector.record_usage` / `totals` signatures; add a focused, typed attempt-journal API in `src/lean_weave/metrics/models.py` for richer outcomes, with no cloned provider framework. Specify how legacy `record_usage` entries contribute to totals and cumulative spend: book each once as an explicitly attributed standalone usage record; never invoke it again for an attempt already finalized by the gateway.
  - Make measured field presence explicit in the new attempt/summary types so missing values do not turn into measured zero through `TokenUsage` defaults. Keep the decoded `usage` object intact, including unknown accounting fields, nested details, nulls, and explicit zeros; use SDK raw response access rather than a lossy dump of modeled fields. If an error exposes a usage object, extract only it. Reject malformed or secret-bearing usage from persistence; preserve valid counters separately where safe and charge conservatively. Never persist the full response, headers, prompts, tool payloads, or reasoning text.
  - Normalize `prompt_tokens` → input, `completion_tokens` → output, and use the reported `total_tokens` unchanged for measured totals. Extract reasoning from `completion_tokens_details.reasoning_tokens`; prefer `prompt_cache_hit_tokens`, falling back to `prompt_tokens_details.cached_tokens` only when the preferred count is unavailable, not when it is zero. No cache subtraction or reasoning double-counting.
  - **Evidence:** Reconciliation row 4; round-trip and compatibility tests in `tests/metrics/test_usage.py` and `tests/contracts/`. **Logging:** Pure mapping/model helpers do not log; expose safe presence and failure codes for gateway use. Add typed, safe operational errors in `src/lean_weave/errors.py` for provider, invalid completion, budget, and journal failures; never expose SDK exception text.

- [x] Task 3: Implement JSONL metrics persistence and querying (depends on 2).
  - Implement `JsonlMetricsCollector` in `src/lean_weave/metrics/jsonl.py`. Reuse `config.loader.resolve_state_dir` and keep the versioned append-only journal at `<resolved state_dir>/metrics/llm-usage.jsonl`. Reject escapes (including journal/lock symlinks) and non-regular files. Feature IDs are record data, not path fragments. Persist reservation and terminal events using stable request/attempt IDs, with one terminal outcome per reservation. Treat exact duplicate events as idempotent, reject conflicting duplicates and orphan terminal events, and fail closed on a partial trailing line. Flush and sync the reservation before allowing transport; sync the parent when creating the journal where supported. Append failures must never permit another send.
  - Own the admission transaction in the journal, not a provider instance: serialize validation, reading current spend, and appending the reservation across all collectors for the same canonical file. Serialize journal writes across feature IDs too. Use a process-shared lock keyed by canonical journal path and a nonblocking OS writer guard for the journal lifecycle (explicitly reject competing writers or unavailable locking); do not hold the admission lock across an HTTP call or retry sleep. Exercise separately constructed collectors, overlapping feature IDs, and lock contention without network calls.
  - Aggregate measured counters separately from local estimate charges and unresolved reservations, scoped by feature with stage/model breakdowns. Include requested model, returned model when available, monotonic elapsed duration per attempt, and retry count (number of retry sends, not the sum of ordinals). Preserve the compatibility `totals` view as measured counters only; expose presence/coverage and budget charges through the richer summary. Ensure a fresh collector reconstructs both totals and spend. Fail closed on unreadable, malformed, conflicting, or unwritable accounting rather than resetting spend.
  - **Evidence:** Reconciliation rows 4–5; `tests/metrics/test_jsonl.py` covers mixed records, restart, bad/duplicate records, containment, and absence of payloads. **Logging:** DEBUG safe journal operation/status, ERROR append/replay failures with safe codes; no row contents.

### Phase 2: Gateway and enforcement

- [x] Task 4: Add DeepSeek request mapping and one-attempt transport (depends on 1–2).
  - Implement `src/lean_weave/providers/deepseek.py` using the SDK's non-streaming chat completion API. Prove the exact client/transport seam against `uv.lock` (`openai` 3.46.0 currently resolves with `httpx2`); do not assume an `httpx` client is compatible. Run raw-envelope and error-path tests through the real locked SDK with an in-memory transport. Configure `max_retries=0` and disable redirect following, including same-origin redirects; assert one send for a redirect or status error, with no request to the redirect target. Load the key only from the configured environment variable; pass timeout and validated base URL explicitly. Document who closes the owned client; do not close caller-injected clients.
  - Build the wire request from caller messages and supported options only. Translate thinking to `extra_body`, explicitly send disabled for supported models when off, validate reasoning-effort combinations, and reject unsupported capabilities without retrying in another mode. Treat model support as an explicit local capability decision, not an extra discovery call; document override behavior and prove it with request-capture tests. Omit inapplicable fields or `None`; never send the local Pydantic validator. Build/validate tool replay without executing or retaining it. Do not add conversation history or hidden prompts. Capture usage from the raw envelope before validating output, including error-body usage when available. Return a parsed result for JSON mode (validate locally if a schema was supplied); reject a schema outside JSON mode before transport.
  - **Evidence:** Reconciliation rows 1–4; `tests/providers/test_deepseek.py` captures outgoing requests and exercises existing `spikes/deepseek_probe/fixtures.py` as data, without importing its live probe into production. **Logging:** DEBUG safe request shape (mode, message count, attempt ID), never SDK bodies, prompts, authorization, or tool/reasoning content.

- [x] Task 5: Enforce cumulative per-feature budgets and bounded retries (depends on 2–4).
  - Implement the booking policy in `src/lean_weave/metrics/budget.py` and wire it into `DeepSeekProvider.complete`. Estimate the serialized provider-bound input including tool schemas, replay, instructions, and response-format overhead using a documented conservative local estimator. Finish local validation/client setup first. Reserve `estimated input + effective output cap` through Task 3's journal admission transaction immediately before **each** send. Document the one-writer-per-journal contract; reservations count against remaining budget while transport is in flight.
  - Replace the reservation with measured spend if all three core counters are valid and `total = input + output`; missing optional cache/reasoning details alone do not make usage incomplete. Otherwise charge the greater of the reservation and the known lower bound (`max(reported total, sum of valid input/output counts)`), without adding optional breakdowns or changing reported values. Keep measured and charge views separate; do not add a fallback charge to the same attempt's measured total when calculating budget spend. Leave unclosed reservations charged on reopen, but report them separately from confirmed attempts/retries because a crash before the send is indistinguishable from a crash after it. Do not retry after a journal failure. Exact exhaustion permits returning a completed valid result but blocks the next send; on feature-budget overshoot persist the outcome then surface a budget error. This is strict **local admission**, not a guarantee of provider billing precision when exact preflight counts or failure usage are unavailable.
  - Retry only bounded transient network/timeout, 429, and transient upstream 5xx failures, plus the documented blank JSON response case. Use bounded backoff and clamp `Retry-After`; count an additional attempt only when sent. Do not retry auth, unsupported requests, other permanent 4xx, malformed payload/schema failures, or budget/journal errors. Finalize/account for each attempt before classifying or scheduling another; include failed attempts with usage in measured totals. Inject clock/sleeper for deterministic tests.
  - **Evidence:** Reconciliation rows 4–5; `tests/metrics/test_budget.py` and `tests/providers/test_deepseek_retries.py` cover first-send denial, exact limit, overshoot, retry exhaustion, known/unknown failed usage, stale reservations, and competing calls within a process. **Logging:** DEBUG admission/outcome metadata, INFO budget exhaustion, WARNING retry and incomplete usage, safe terminal errors through existing error handling.

- [x] Task 6: Add deterministic mock and injection wiring (depends on 3–5).
  - Add `MockLLMProvider` in `src/lean_weave/providers/mock.py`, implementing the existing Protocol with a scripted sequence of responses and typed failures, explicit synthetic usage, and captured requests for offline assertions. Script exhaustion fails clearly. Test the real retry/budget path with a scripted transport under `DeepSeekProvider`, not a duplicate implementation in the mock. Label mock data as synthetic, not measured DeepSeek usage.
  - Add a focused on-demand construction seam using `src/lean_weave/app.py` and the provider module. Preserve precedence for an injected `LLMProvider`; if constructing the production provider, use an explicitly injected compatible attempt journal or the default JSONL journal. Keep the legacy `MetricsCollector` slot compatible; if it is the caller's only supplied accounting implementation and cannot reserve atomically, raise a safe dependency error rather than ignoring it or disabling budgets. Do not eagerly create an SDK client, require a key, open the journal, or perform network activity in `build_app_context`, help, `init`, `config show`, `doctor`, or `feature`.
  - **Evidence:** Reconciliation rows 5–6; `tests/providers/test_mock.py` and `tests/test_app_context.py` cover deterministic sequence, injected overrides, and offline defaults. **Logging:** DEBUG implementation selection and script progress (IDs/status only).

### Phase 3: Connection check and evidence

- [x] Task 7: Add the explicit connection check (depends on 6).
  - Register `wl llm check` in `src/lean_weave/cli/app.py` and implement it in `cli/commands/llm.py` using `command_context` and the same production provider/ledger path. Use a typed gateway option that only lowers configured retry limits (set it to zero for the probe), not a duplicate provider or global config mutation. Make one fixed, non-thinking, non-tool request with at most `min(configured cap, 32)` output tokens. Use the reserved probe feature ID and the configured cumulative budget; keep the selected model and timeout. Do not accept arbitrary prompts or tool input.
  - Print only safe status, model, and available usage counts; suppress raw reply text and report missing usage explicitly. Preserve exit codes: 0 success, 1 operational failure (including missing key or budget exhaustion), 3 config failure, 2 argument errors. Help stays offline. Neither `doctor` nor feature commands invoke this check.
  - **Evidence:** Reconciliation row 6; `tests/cli/test_llm.py` invokes the registered app with `CliRunner`, injects transport at the lazy composition boundary, and uses actual YAML generated by `wl init`. With YAML retries greater than zero, a 429 still sends once and records one finalized probe attempt. An exhausted probe sends nothing and leaves normal feature budgets untouched. Assert both failure stderr and success stdout independently of logs; check that SDK-supplied model text cannot echo the configured key or terminal control characters. **Logging:** INFO safe completion status and duration, ERROR safe failure code on stderr.

- [x] Task 8: Complete offline cross-layer coverage (depends on 5–7).
  - Extend `tests/providers/`, `tests/metrics/`, and `tests/cli/` to assert the end-to-end combination of retryable failure with usage, subsequent measured success, optional cache/reasoning fields, and a different-stage call denied before transport after restart. Also assert bounded backoff, no SDK-level retries, malformed output charged before rejection, and a clean feature ID unaffected by another feature's spend.
  - Run `uv run --offline --no-sync pytest --ignore=tests/spikes` in the prepared core environment and `uv run --offline --no-sync pytest` in the prepared spikes environment. Re-run the gateway tests with the configured key unset and with a dummy value. Keep `tests/conftest.py`'s network guard; use no network markers for gateway tests. Assert secret-bearing provider messages and reasoning do not appear in stdout/stderr/logs or JSONL; assert injected code never calls the external service.
  - **Evidence:** All reconciliation rows; actual request counts and persisted/reloaded ledger assertions. **Logging:** Assert levels and safe metadata via test capture; no production logging added solely for tests.

- [x] Task 9: Document and verify the installed gateway (depends on 7–8).
  - Run the mandatory `/aif-docs` checkpoint for `docs/cli.md`, `docs/getting-started.md`, and `docs/architecture.md`. Document config defaults/limits, supported combinations, retry boundaries, JSONL format and single-writer scope, reported versus estimated usage, cache treatment, and the limits of preflight estimates. Document `uv run wl llm check` as an opt-in, potentially billable smoke test requiring a configured environment key. Verify the package builds and both aliases expose the command.
  - With explicitly available real credentials and approval to incur a small request, run one live check. Verify success and compare the safe output with the corresponding journal record; capture only sanitized model/usage/status evidence. If credentials or permission are absent, mark live acceptance **unverified**, not passed; offline verification remains separate. No live run in automated tests.
  - **Evidence:** Original Request acceptance criteria; packaging checks and manual smoke procedure. **Logging:** Document safe DEBUG/INFO fields, retry warnings, and failure codes; never publish raw provider responses or credentials.
  - **Run status:** Offline verification passed. Live acceptance is **unverified** in this run because no real credentials or spend approval were provided; it must not be reported as passed.

## Acceptance Criteria Mapping

| Criterion | Planned evidence |
|---|---|
| Real DeepSeek request can execute | Task 9: opt-in one-attempt `wl llm check`; success plus recorded probe usage when reported |
| Usage recorded and aggregated | Tasks 2–3, 8: exact raw usage round-trip; measured versus estimated views; cache/reasoning subsets; successful and failed attempt accounting |
| Exhaustion stops additional requests | Tasks 5, 8: exact-boundary, overshoot, retry-denial, feature isolation, and reopen transport-count assertions |
| Mock tests run without network access | Tasks 6–8: scripted mock/transport under existing pytest socket guard |
| Gateway independently testable | Tasks 4–8: direct provider and ledger tests without CLI orchestration or coding-agent runtime |
