# v1-01 MVP 1 Acceptance Criteria (Architecture Gate)

This is an **architecture validation** gate, not a product feature gate. No production orchestrator CLI beyond spikes.

## Checklist

| # | Criterion | Evidence | Status |
|---|-----------|----------|--------|
| 1 | Repo inventory + methodology reuse + token principles published | `.ai-factory/references/v1-01-repo-inventory.md`, `v1-01-aif-methodology-reuse.md`, `v1-01-token-budget-principles.md` | [x] |
| 2 | Spike tooling runnable via uv | `pyproject.toml`, `spikes/README.md`, `.gitignore` | [x] |
| 3 | DeepSeek validation note + offline probe tests | `v1-01-deepseek-validation.md`, `spikes/deepseek_probe/`, `tests/spikes/test_deepseek_probe.py` | [x] |
| 4 | LangGraph interrupt/resume/sqlite validated | `v1-01-langgraph-validation.md`, `tests/spikes/test_langgraph_probe.py` | [x] |
| 5 | Orchestrator comparison + ADR-002 decision | `v1-01-orchestrator-comparison.md`, `adr/ADR-002-*.md` | [x] |
| 6 | Component contracts (docs + Protocols) | `v1-01-component-contracts.md`, `src/lean_weave/contracts/`, `tests/contracts/` | [x] |
| 7 | State schemas (docs + Pydantic) | `v1-01-state-schemas.md`, `src/lean_weave/schemas/`, `tests/schemas/` | [x] |
| 8 | Isolation/security includes git-absent and git-present | `v1-01-isolation-security.md` | [x] |
| 9 | Project structure + LangGraph deferred design | `v1-01-project-structure.md` | [x] |
| 10 | DESCRIPTION + ARCHITECTURE + ADRs + risks | `.ai-factory/DESCRIPTION.md`, `ARCHITECTURE.md`, `adr/ADR-00{1-4}-*.md`, `v1-01-risk-register.md` | [x] |
| 11 | Human docs page + README blurb | `docs/architecture.md`, `README.md` | [x] |
| 12 | Pytest suite green | Command below | [x] |
| 13 | No production orchestrator CLI beyond spikes | Confirmed: only `lean_weave:main` hello stub; spikes under `spikes/` | [x] |
| 14 | Git-absent StateStore path policy documented | Isolation §(a); workspace currently has no `.git` | [x] |
| 15 | Git-present policy documented for future | Isolation §(b); acceptance requires both combos documented | [x] |

## Pytest evidence

```text
Command: uv run pytest tests/spikes tests/schemas tests/contracts
Result: 14 passed (2026-11-08); exit 0
Notes: DeepSeek live cases skipped (DEEPSEEK_API_KEY absent); offline fixtures asserted.
Production CLI remains lean_weave:main hello stub only; probes under spikes/*/probe.py.
```

## Explicit non-goals (must remain true)

- No MCP/ACP/Docker/workers/multi-agent production code
- No nested autonomous agent session launcher
- LangGraph not required at runtime for MVP product path
