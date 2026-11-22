# Phase 4: Operations, acceptance and documentation

Plan: [index.md](index.md)  
Tasks: 10–12  
Depends on: Phases 1–3 / Tasks 1–8 for Task 10, plus Task 9 for Task 11

## Objective

Expose explicit offline operations and prove reuse, compatibility, boundedness and isolation with real fixtures.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/cli/{app,runtime}.py` | `app.add_typer`, `SafeGroup`, `command_context` | Existing lazy setup and error handling. |
| `src/lean_weave/cli/commands/index.py` | `status_command`, `clear_command`, `_fail` | Stable health output and explicit destructive confirmation pattern. |
| `benchmarks/retrieval.py` | `load_cases`, `materialize_repository` | Real existing fixture and offline evaluation patterns. |
| `tests/conftest.py`, `tests/cli/test_feature_offline_e2e.py` | offline test harness and feature fixture | Prevent new calls and compare deterministic product behavior. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/cli/{app.py,commands/cache.py}` | modify/create | Register lazy `wl cache` group and display typed operational results. |
| `src/lean_weave/context_cache/maintenance.py`, `src/lean_weave/app.py` | create/modify | Build bounded offline seeder and resolve workspace explicitly. |
| `benchmarks/context_cache.py`, `tests/benchmarks/test_context_cache.py` | create | Cold/warm/disabled measurement on existing fixture corpus. |
| `tests/cli/test_cache.py`, `tests/context_cache/test_offline_e2e.py` | create | CLI and end-to-end regression checks. |
| `docs/context-cache.md`, `docs/{cli,architecture,context-retrieval,context-packing,benchmarks}.md`, `README.md` | create/modify | Mandatory documentation checkpoint. |

## Task 10: Expose offline cache maintenance

### Intent

Make health, counters, clearing and bounded rebuilding explicit, checkout-local, and useful without any LLM credentials.

### Implementation Steps

1. Register `cache_app` via `cli/app.py`; use `command_context()` only inside command bodies. Factory binds cwd as current checkout (same as `wl index`); no path override, no peer options. Nested help uses neither config nor cache. Define stable plain output: `status` shows enabled/health/schema/entry count/DB bytes/limits and migration pending when applicable; `stats` shows fixed ordered totals plus per-kind counts. Do not print roots, task data, arbitrary paths or DB errors.
2. `clear --yes` removes only this checkout's ContextCache data leaves even if configuration is disabled, so an operator can purge after disabling. Without `--yes` exit 2 before loading config. Disabled status/stats show disabled and zero/unavailable without opening storage; rebuild while disabled exits 2 with an enablement hint. Ready/missing/disabled inspections and idempotent clear exit 0; unsafe/corrupt/incompatible/busy/capacity/unavailable or uncertain actionable failures exit 1 with stable guidance, except rebuild can recover diagnosed corruption or old capacity state as specified in Phase 1.
3. `wl cache rebuild`: in `maintenance.py` open one current complete strict retrieval scan and try `CodeIndex.read_generation_for_retrieval_scan` before taking the ContextCache maintenance lease. Detach the checked generation and release the index lease before calling `ContextCache.rebuild`. Inside the seed callback, iterate sorted admitted Python paths, prime shared facts (Task 4) and strict packing spans, then derive import/map entries directly from the detached checked generation when available. Use production key/codec builders; route only writes to the pending writer, never through lazy factories that reopen a live DB. Report `map_seed=ready|skipped` and capacity skips; a missing/stale/inaccessible index does not cause automatic maintenance. Candidate/rank entries are task-specific and warm on demand. Pass `scan.revalidate` to the replacement step while the scan is open. Refuse incomplete or changed scans without replacing an existing good cache; no prompt material, network, state writes, or LLM dependencies.

### Required Interfaces and Contracts

- `status`/`stats` are read-only; `clear` and `rebuild` do not touch CodeIndex or other namespaces. The explicit maintenance factory may bypass disabled mode only for confirmed clear.

### Requirement Evidence

Original Request CLI, 11–17 and 19–20; existing `wl index` UX is a pattern, not a new index command or alias.

### Error Handling and Logging

INFO command/result and bounded counts; WARN typed reason and safe next step. Stable exit codes distinguish usage from health failures; no paths or secrets.

### Tests

`tests/cli/test_cache.py`: all four commands with private temporary XDG, invalid config during help, disabled clear and rejected rebuild, confirmations, known-old/corrupt/unknown version, source race, checked versus skipped map seed, CodeIndex unchanged and no API key or provider construction. After one rebuild, use production factories to verify a no-index stage fact hit and a strict pack span hit; candidate/rank first use still misses. Use a barrier/lease assertion to show rebuild does not open CodeIndex from inside the exclusive cache lease.

### Acceptance Criteria

Operators can inspect, clear, recover and rebuild safely offline, with deterministic command semantics and no checkout writes.

### Verification

`uv run pytest tests/cli/test_cache.py tests/cli/test_cli.py tests/cli/test_index.py` — all pass.

## Task 11: Verify behavior and measure local reuse

### Intent

Measure saved deterministic work rather than assuming faster execution or fewer transmitted tokens.

### Implementation Steps

1. Add `python -m benchmarks.context_cache --json` using existing `benchmarks/fixtures/retrieval/cases.json` and `benchmarks/retrieval.py::materialize_repository`; include `multi_file_banner` and `bug_fix_add`. Use two separately reported workloads through production factories: (a) no CodeIndex, for stage/retrieval extraction and packing spans; (b) checked CodeIndex prepared outside timing, for resolver and indexed map/L0 header reuse. Within each workload compare disabled, empty-enabled (reset only ContextCache between samples), and warm (prime outside timing) with identical repository/config/task inputs, fresh adapter instances and temporary XDG. Also run an enabled map case with a dependency edit and explicit index refresh between passes. Record parity signatures per workload, per-kind hits, parser/resolver/renderer/base-scoring/rank/span call counts, DB bytes and local wall/CPU time. Report median of at least five fixed iterations each; CI asserts targeted call-count reductions, not wall time or parity between different index modes.
2. Report equal serialized payloads/estimated budget numbers as compatibility evidence, not API usage measurements. Report `llm_calls=0` and `api_tokens_measured=false`; do not claim savings. If overhead erases timing benefit, report it and keep opt-in defaults, with evidence showing which operations actually stopped repeating.
3. Add e2e tests using the checked-in `multi_file_banner` fixture and current production app factories: compare cold/warm/disabled rank and packed results; edit `src/config.py` and compare live results and targeted invalidation; verify unrelated facts remain hits. Add feature worktree test around the existing offline driver: warm a same-root implement/retry path, test resume and another checkout, compare captured requests and unchanged number of existing gateway calls; use fake gateway and sentinels that fail on any new provider call/ledger writes from cache maintenance. Check concurrent CLI/read-write recovery via subprocesses under a single temp namespace, and no hidden peer lookup when disabled/default.

### Required Interfaces and Contracts

- Benchmark output is developer tooling and does not add product CLI or optional dependencies; use bounded local fixtures and stdlib timing. Counters and call spies, not timing thresholds, enforce elimination of repeated work.

### Requirement Evidence

Original Request testing, token-efficiency and acceptance sections; v2-03 fixture and v2-04 payload/guard contracts.

### Error Handling and Logging

Benchmark fails explicitly on mismatched outputs or failed fixture setup; summarize mode, counts and timings without dumping source/task text. Product logs remain unchanged for disabled mode.

### Tests

`tests/benchmarks/test_context_cache.py` asserts mode/metric shape, call deltas, serialization parity and no network; `test_offline_e2e.py` exercises real fixture and feature worktree behavior. No network-marked tests in required verification.

### Acceptance Criteria

Enabled warm runs provably skip repeated deterministic work, preserve outputs and introduce no additional LLM calls; stale and cross-worktree entries are never used improperly.

### Verification

`uv run python -m benchmarks.context_cache --json` — identical compatibility signatures, positive warm reuse and zero LLM calls. `uv run pytest -m 'not network'` — offline suite passes.

## Task 12: Document configuration and operations

### Intent

Make rollout, limits, failure recovery and measurement clear without overstating what is reused.

### Implementation Steps

1. At the mandatory `/aif-docs` checkpoint, add `docs/context-cache.md` with a working `context.cache` config snippet, four commands and exit codes, external storage layout and per-checkout bounds including transient journal/pending overhead. Include how to disable and purge, migration and corruption recovery, explicit index vs ContextCache maintenance, and why new worktrees start cold by default.
2. Link it from `README.md` and `docs/{cli,architecture,context-retrieval,context-packing,benchmarks}.md`. Explain cacheable metadata, fresh source scans and budgets, optional trusted Python peer reuse and its restrictions, workload metrics separate from API token estimates or actual usage, and no model-response caching/background service. No roadmap change.

### Required Interfaces and Contracts

- Examples target `.lean-weave/config.yaml`, not AI Factory config. Examples must work without `DEEPSEEK_API_KEY`; do not advertise private peer APIs as CLI options.

### Requirement Evidence

Original Request CLI, token-efficiency, isolation and out-of-scope sections; config `workflow.plan_docs: yes`.

### Error Handling and Logging

Document log levels and safe diagnostic fields; do not include pasted payloads, user environment paths, or unredacted secrets.

### Tests

Follow documented enable → rebuild → status/stats → clear in a disposable checkout; retain executable examples in CLI tests, and verify links.

### Acceptance Criteria

An offline operator can enable, diagnose, clear and rebuild independently of the source index, and understand what cache hits do not save.

### Verification

`uv run pytest tests/cli/test_cache.py` — examples and exit-code expectations pass; verify all new relative documentation links resolve.

## Phase Risks and Mitigations

Do not report API savings based on a cache hit, and do not silently rebuild the index or harvest task inputs for seeding.

## Phase Completion Checklist

All three tasks, the offline suite and developer benchmark pass. Complete the documentation checkpoint before marking Task 12 in `index.md`.
