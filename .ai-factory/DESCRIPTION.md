# lean-weave

Token-efficient AI coding pipeline that automates feature development with structured workflow state, DeepSeek LLM calls, and deterministic local verification — minimizing total tokens while preserving implementation quality.

## Stack

- **Language:** Python ≥3.12
- **Packaging:** uv (`pyproject.toml`, `src/lean_weave`)
- **CLI/configuration foundation:** Typer (Click-based release), Click, PyYAML; product configuration is `.lean-weave/config.yaml` (separate from AI Factory tooling). Executables: `wl` and `lean-weave`.
- **Context retrieval:** deterministic, LLM-free `context/` package — bounded repository inventory with `pathspec` ignore semantics, AST symbols/imports, literal search (`rg` stdin with Python fallback), optional preview Git recency, lexicographic ranking, provenance, progressive expansion, and prompt budgets.
- **Code tools:** standalone `executor/` and `verify/` APIs for bounded reads, exact single-file patches, approved checks, and read-only Git path/status inspection. YAML does not authorize writes or spawns; the workflow binds them through a caller approval snapshot, with no tool CLI.
- **Workflow:** minimal deterministic `workflow/` orchestrator (plan → implement → verify → bounded retry) with contained atomic checkpoints under `.lean-weave/workflow/`, typed results, plan policy, bounded tool loops, and per-scope token/call budgets. Public CLI wiring via `workflow/session.py` and `worktree/` isolation (`wl feature` / resume / status / cancel / result).
- **Code index (V2):** optional disposable SQLite cache under `${XDG_CACHE_HOME:-~/.cache}/lean-weave/code-index/<checkout-key>/`; strict descriptor-relative admission, AST fact extraction, content-hash incremental reconcile, checked lookup, and per-checkout isolation. Explicit offline commands `wl index build/update/status/clear`; no LLM calls, no prompt authority, no writes into the checkout.
- **Repository map (V2):** `repository_map/` builds an in-memory static import graph from a checked index generation; budgeted `wl map show|module|dependencies|references` and optional `wl context build --indexed-map`. No LLM, no automatic index maintenance, no workflow orchestrator changes.
- **Task-aware retrieval (V2):** `context/retriever.py` ranks admitted files/definitions with hybrid signals; opt-in `wl context rank|explain|search --task-aware` and `DeterministicContextProvider.retrieve`. Stage builds unchanged; evaluation via `python -m benchmarks.retrieval`.
- **Token-aware packing (V2):** `context/packer.py` composes revision-checked ranked source under independent section/request budgets (L0 map, L1 definitions, bounded explicit L2 expansion); opt-in `wl context build|inspect --packed` and `wl context budget`; explicit guarded DeepSeek adapter. No embeddings, LLM summaries, or workflow-stage changes.
- **Context cache (V2):** opt-in checkout-local `context_cache/` SQLite store for deterministic facts, import relations, map rows, ranking projections, and packing spans; live keys/proofs, bounded LRU storage, epochs, explicit `wl cache status|stats|clear|rebuild`, and optional trusted peer facts. No model-response caching, distributed store, background work, or prompt authority.
- **Benchmarks (developer tooling):** `benchmarks/` fixture corpus, offline MockTransport CLI driver, metric projection, and optional explicit live runner — not a product `wl` command.
- **LLM:** DeepSeek OpenAI-compatible API (`deepseek-flash` / `deepseek-v4-pro`) via the locked `openai` SDK client; centralized budgeted `DeepSeekProvider` with an atomic JSONL usage ledger
- **Orchestrator (MVP 1):** minimal deterministic state machine (LangGraph deferred; spike retained)
- **Schemas:** Pydantic FeatureState / FeaturePlan / tasks / VerificationResult

## MVP 1 exclusions

No MCP, ACP, Docker orchestration, background workers, or multi-agent architecture.

## Pointers

- Architecture: `.ai-factory/ARCHITECTURE.md`
- Plan: `.ai-factory/plans/v1-01-architecture.md`
- Human docs: `docs/getting-started.md`, `docs/architecture.md`, `docs/code-index.md`, `docs/context-retrieval.md`
- Roadmap: `.ai-factory/ROADMAP.md`
