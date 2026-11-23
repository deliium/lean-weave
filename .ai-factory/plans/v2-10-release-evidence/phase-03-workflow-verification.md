# Phase 3: Workflow Verification and Live Disposition

Plan: [index.md](index.md)  
Tasks: 5–6  
Depends on: Task 2's verifier contract for implementation; real live execution waits for Tasks 4 and 5 to pass

## Objective

Verify the clean commit and installed wheel without weakening the offline suite. Produce a separate, truthful live disposition under explicit policy and admission.

## Current-Code Evidence

- `tests/conftest.py` blocks most network by default in test bodies, not as a child-process sandbox. `pyproject.toml` sets pytest source paths, so pytest is not installed-wheel proof.
- `src/lean_weave/benchmark/runner.py::{run_matrix,run_offline_arm,run_live_matrix}`, `benchmark/models.py::WORKFLOW_VARIANTS`, and `benchmarks/validation/v2-08-workflow-thresholds.json` provide paired scripted workflow and correctness gates. Stock V2 uses V1 fallback and does not enable dependency coverage.
- `src/lean_weave/benchmark/live.py::{load_pricing,validate_live_request,run_live_case}` provides live admission, budgets, deadline and checked-in fixture execution. `docs/benchmarks.md` → “Paired live opt-in” gives the command. Gateway counters belong to actual attempts; scripted totals and local estimates are separate.

## Files to Change

- `benchmarks/release_evidence.py`: `offline` and `live` subcommands and evidence envelopes.
- `tests/benchmarks/test_retrieval.py`, `tests/benchmarks/test_retrieval_pack.py` and other callers identified by audit: migrate historical-case-dependent regression inputs to development-only or original synthetic cases, preserving assertions and historical catalog/results.
- `tests/test_release_evidence.py`: isolation, failure capture, live admission and measured-usage tests.
- `tests/release_pytest_ledger.py` and `tests/benchmarks/fixtures/`: add a release collection/outcome plugin and independent retrieval catalog fixtures.
- `docs/v2-release-evidence.md`: commands, regression inventory and live-policy interpretation.

## Task 5: Verify the offline workflow

### Intent and implementation

1. Before retrieval-catalog tests load data, monkeypatch `lean_weave.benchmark.retrieval.CASES_PATH` to a committed development-only catalog under `tests/benchmarks/fixtures/`, with a development/synthetic `TREES` root where needed. `load_cases()` validates and fingerprints every input case before callers can filter. Audit direct calls, `run_comparison`, and `runner.run_retrieval_matrix`. Replace named historic-case checks with independent equivalents while preserving behavior, the old catalog and results. Test that automatic regressions never open the historical catalog; no deselection or post-result tuning.
2. From a third verified detached worktree, use the Phase 1 hashed export with explicit `--group dev --group spikes` (test collection only). Verify pinned `rg` on a bounded `PATH`; missing ripgrep must not silently skip tests. Use a pinned Linux network namespace launcher (`unshare --user --map-root-user --net -- <command>`), without credentials, with external HOME/temp/cache and `PYTHONDONTWRITEBYTECODE=1`. Verify it creates a separate namespace without external routes/interfaces and that a child cannot connect to a loopback-only listener in the parent namespace; block if prerequisites fail. Run `<dev-python> -m pytest -p tests.release_pytest_ledger -m 'not network' -p no:cacheprovider --junitxml=<external>/offline.xml` from the detached worktree with third-party plugin autoload disabled. The committed plugin records exact `item.nodeid` values from `pytest_collection_finish`, collection errors and `pytest_runtest_logreport` setup/call/teardown outcomes. Compare to a `--collect-only` pass with identical settings; account for all selected IDs and preregistered skips, reject duplicate/missing outcomes and skipped/xfail required nodes. JUnit is supplemental. Verify source and wheel before/after; preserve failures, exits and bounded logs.
3. Required coverage inventory: `tests/workflow/{test_orchestrator,test_state,test_budget,test_context_lifecycle,test_context_expansion,test_feature_e2e}.py`, `tests/providers/test_deepseek*.py`, `tests/{context,context_cache,retrieval_dependencies,worktree,verify,executor}/`, `tests/cli/test_feature_{offline,recovery,isolation,context}_e2e.py`. Before freeze, map named required safety/reliability nodes to approved tool/write scope, no replay of uncertain effects, strict-V2 dependency scope/refusal, budget boundary, verification repair/retry and checkout isolation; absence or skipped required node blocks. Do not infer strict dependency-policy workflow coverage from a V2-fallback matrix.
4. In the verified wheel environment outside Git, under the same network boundary, smoke both CLI aliases and run installed `wl benchmark run --suite workflow --variant v1 --variant v2 --repeats 3 --output <external>/workflow.json`. Run installed `wl benchmark compare "$E/workflow.json" "$E/workflow.json" --axis strategy --baseline-variant v1 --candidate-variant v2 --thresholds "$R/benchmarks/validation/v2-08-workflow-thresholds.json" --json`, with `$R` the verified detached source and `$E` the external run root. Require all declared arms, fixture pins, correctness gates, preserved failed attempts, real repair/retry and comparable verifier/limits. Record `installed-wheel` separately from `source-regression`, not two equivalent runtime claims.

### Contract, failure and logging

`OfflineReceiptV1` binds source pytest and installed paired records from a single artifact ID, with commands/exit/counts, required-node and fixture-manifest digests, active configuration and test/benchmark output hashes. INFO: observed counts, smoke and gate outcome; DEBUG: normalized commands and coverage IDs; ERROR: `offline_failed`, `required_node_missing`, `schedule_incomplete`, `artifact_changed`. Preserve redacted bounded diagnostics under the external root, not raw request/prompt bodies.

### Requirement evidence, tests and acceptance

Original Request C, acceptance 4, historical preservation; v2-08 paired correctness protocol and v2-09 known-holdout restrictions. `tests/test_release_evidence.py` uses fixtures for missing/skipped required nodes, incomplete schedule, report mutation and changed wheel. Verify targeted checks, then `offline` on the clean committed wheel and full suite. Gate is pass only when every required test and installed matrix gate is valid and green; absence of an egress-denied environment is blocked.

## Task 6: Record live policy and measured usage

### Intent and implementation

1. Require a frozen digest-bound policy document (`live: optional|required`, owner, approval/effective revision, any required live gates). Under the request live is optional unless an approved release policy explicitly requires it; ambiguous or missing policy evidence cannot be used to waive a required gate. `live` without separate affirmative operator authorization for this artifact/config, available `DEEPSEEK_API_KEY`, verified pricing/config/caps or approved fixtures must not dispatch. Write `Live: Not evaluated` with reason; never log key or config bodies.
2. For authorized execution, only after deterministic prerequisites and blind gate pass, require a passing no-network admission rehearsal against the exact installed runner, including fixed verifier timeout/output cap, complete effective config, finite deadline, strict pricing (reject duplicate keys), retry-inclusive cost/token reservation, deadline enforcement for children and oracles, and safe stop on unverifiable usage/model drift. Current `live.py`/`runner.py` do not establish all these controls: in particular, the ordinary child does not install the gateway post-settlement hook, oracle timeout can extend beyond the remaining deadline, and timeout rows may lack journal evidence. If any control is unproven, write `not_evaluated` / `live_runner_not_qualified`; do not spend or patch product guardrails during an evidence run. If live is required, obtain a separately scoped runner fix, commit and freeze a new artifact before evaluation.
3. Only for a qualified runner invoke existing paired `wl benchmark run --live --suite workflow --case bug_fix --case multi_file_feature --variant v1 --variant v2 --repeats <frozen-count> --live-config-dir <private-dir> --pricing <private-table> --max-tokens <cap> --max-cost-usd <cap> --deadline-s <cap> --output <external>/live.json` from the verified wheel. Freeze the same fixture/source revisions, model/settings, checker/time limits, order and equal conditions across arms; record actual mode switches, including any V2 fallback, rather than claiming strict dependency-enabled live evidence. Retain all failed attempts/retries and comparable verification outcomes; record verified fixture/conditions independently when absent from live rows. Use immutable operator-approved rates and existing admission reservations including both arms and retries; no bypass, auto-authorization or blind inputs. Unknown provider billing remains unknown. Keep raw runner files restricted; only redacted schedules and aggregates are public.
4. Aggregate provider-reported input/output/total usage for every observed dispatch, including failed billed tasks and retries. Exclude explicit verified no-send bookings and never sum local estimates, reservations or duplicate journal rows. Missing attempts/counters, inconsistent totals, unknown dispatch, model drift, zero successes or incompatible pairs make the per-success ratio unavailable, not zero. Otherwise divide measured total by verified successful task count and show all schedule denominators. A measured savings claim needs complete comparable usage and preserved success/correctness, and applies only to the evaluated configuration.

### Contract, failure and logging

`LiveReceiptV1` binds policy and run IDs, admission disposition, artifact, schedule/config/pricing/fixture hashes, and actual/estimated coverage. State is `not_evaluated` only when no dispatch is established; use `invalid_or_incomplete` if dispatch or usage cannot be reconciled. A verified blocking safety/workflow regression remains `evaluated_fail` even with missing counters. Only comparable, complete, eligible results are `evaluated_pass`. Under optional live, verified non-dispatch or valid observed incomplete usage alone does not block if no blocking issue is known; invalid run binding, missing receipt, unknown safety or unauthorized dispatch cannot be waived. Required live accepts only valid `evaluated_pass`. INFO: decision, caps and usage coverage; DEBUG: sanitized IDs and counters; ERROR: drift, admission refusal or incompleteness; never emit credentials or raw payloads.

### Requirement evidence, tests and acceptance

Original Request D and release gate; `docs/benchmarks.md` “Paired live opt-in”; `tests/benchmarks/test_live_limits.py` and `tests/providers/test_deepseek_retries.py`. Test absent approval/key/policy and exceeded cap with a spy asserting zero calls, fake successful/missing usage, failed attempts retained in numerator, zero denominator, and required vs optional states. Verify `uv run --locked --offline pytest tests/test_release_evidence.py tests/benchmarks/test_live_limits.py`; an actual live run is never a test prerequisite and is not authorized by this plan.

## Phase Risks and Completion

- A mocked success only establishes functional behavior; egress control is required in addition to pytest monkeypatching. Legacy holdout fixtures stay archived as recorded, not active regression inputs.
- Check off tooling after synthetic verification. After both tooling checkpoints the operator records the full offline and explicit live receipts or their blocked/failure dispositions. Checkboxes belong only to the index.
