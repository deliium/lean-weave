[← Benchmarks](benchmarks.md) · [Back to README](../README.md)

# MVP 1 Readiness

Captured: 2026-11-21  
Plan: `v1-08-e2e-validation`  
Offline baseline: [`benchmarks/baselines/v1-offline-baseline.json`](../benchmarks/baselines/v1-offline-baseline.json)

## Verdicts

| Gate | Verdict | Evidence |
|------|---------|----------|
| Offline CLI acceptance | **pass** | `uv run --locked pytest` over `tests/cli/test_feature_*.py` and `tests/benchmarks/` (63 focused tests green in capture). Seven-case offline runner correctness `7/7`. |
| Live end-to-end DeepSeek | **not run** | No explicit `--live` authorization in this capture. Gap recorded; no fabricated live token numbers. Does not invalidate offline pass. |
| Approval / recovery safety | **pass** | Recovery and isolation suites cover repair attribution, non-retryable failures, budget stops, cooperative cancel, committed-unconfirmed inspection, dirty-origin preservation, review token/fingerprint drift, check-definition/limit/`state_dir` authority drift, and workspace-code refusal without backend. |

## Acceptance criteria

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Feature execution through `wl` | **pass** (offline injected gateway + installed `wl` smoke) |
| 2 | Offline end-to-end tests | **pass** |
| 3 | Workflow recovery | **pass** |
| 4 | Safety restrictions | **pass** (authority digest + isolation tests) |
| 5 | Token accounting consistency | **pass** (shared journal identity + `benchmarks/results.py`) |
| 6 | Reproducible fixtures | **pass** (pinned HEADs, dual materialization) |
| 7 | Baseline or gaps | **pass with live gap** (offline JSON retained; live not run) |
| 8 | Documentation | **pass** (`docs/benchmarks.md` and CLI/worktree updates) |
| 9 | MVP 1 readiness documented | **pass** (this file) |

**Overall offline MVP readiness: pass.**  
**Overall live readiness: not run** (roadmap real-run evidence remains open until an authorized DeepSeek capture).

## What works

- Public `wl feature --auto` / `--review` with real worktrees, revision-checked patches, and data-only oracles
- Shared production journal between gateway and workflow budget
- Bounded retries with recognized diagnostics; generic failures do not repair
- Dirty origin preservation; authority drift refuses resume before dispatch
- Versioned metric projection with unavailable-vs-zero semantics

## Limits and non-goals

- No public workspace-code isolation backend
- Concurrent external Git/editor races unsupported
- Live DeepSeek baseline not captured here
- Hardening and LangGraph revisit remain separate roadmap items

## Reproduce

```bash
uv run --locked pytest tests/benchmarks/ \
  tests/cli/test_feature_offline_e2e.py \
  tests/cli/test_feature_recovery_e2e.py \
  tests/cli/test_feature_isolation_e2e.py \
  tests/cli/test_feature_e2e.py
```

Live (operator-controlled only): see [Benchmarks](benchmarks.md).
