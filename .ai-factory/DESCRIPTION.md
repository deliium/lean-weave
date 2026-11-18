# lean-weave

Token-efficient AI coding pipeline that automates feature development with structured workflow state, DeepSeek LLM calls, and deterministic local verification — minimizing total tokens while preserving implementation quality.

## Stack

- **Language:** Python ≥3.12
- **Packaging:** uv (`pyproject.toml`, `src/lean_weave`)
- **CLI/configuration foundation:** Typer (Click-based release), Click, PyYAML; product configuration is `.lean-weave/config.yaml` (separate from AI Factory tooling). Executables: `wl` and `lean-weave`.
- **LLM:** DeepSeek OpenAI-compatible API (`deepseek-flash` / `deepseek-v4-pro`) via the locked `openai` SDK client; centralized budgeted `DeepSeekProvider` with an atomic JSONL usage ledger
- **Orchestrator (MVP 1):** minimal deterministic state machine (LangGraph deferred; spike retained)
- **Schemas:** Pydantic FeatureState / FeaturePlan / tasks / VerificationResult

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent architecture.

## Pointers

- Architecture: `.ai-factory/ARCHITECTURE.md`
- Plan: `.ai-factory/plans/v1-01-architecture.md`
- Human docs: `docs/getting-started.md`, `docs/architecture.md`
- Roadmap: `.ai-factory/ROADMAP.md`
