# Project Rules

> Short, actionable rules and conventions for this project. Loaded automatically by /aif-implement.

## Rules

- Keep all `import` and `from ... import ...` statements at the top of the module; never import inside functions, methods, or classes unless a documented exception (circular import or optional dependency) is required.
- Prefer deterministic operations (file select, verify, schema checks) over LLM calls whenever both can satisfy the requirement.
- Keep durable workflow state as structured FeatureState (and related schemas); never persist full chat history as source of truth.
- Isolate LLM context by stage (plan / implement / retry); never load other-stage transcripts, secrets, or wholesale AI Factory skill trees into prompts.
- Use digests and allowlisted paths for ContextProvider; full file bodies only when allowlisted and within size/token budgets.
- MVP 1 forbids MCP clients, ACP clients, Docker orchestration, background workers, multi-agent routers, and nested autonomous agent / `/aif-*` session launches from the pipeline.
- WorkflowOrchestrator for MVP 1 is the minimal deterministic state machine; do not add LangGraph as a runtime dependency unless ADR-002 is explicitly revisited.
- LLMProvider must support DeepSeek thinking on/off, tools, `response_format`, and when tools are present must echo prior `reasoning_content` on assistant replay; store reasoning only for in-flight tool loops.
- API keys and secrets are environment-only (`DEEPSEEK_API_KEY`); never write them to StateStore, plans, logs, or committed files; redact secrets in logs.
- CodeExecutor uses local subprocess with cwd jail and no `shell=True`; tests must not use network unless explicitly opted in.
- When git is absent, StateStore lives only under `.lean-weave/` with no git ops; when git is present, forbid destructive git commands and do not push by default in MVP.
- Canonical product entrypoint is `lean_weave:main`; do not grow `src/main.py` as a second product CLI.
- Spikes under `spikes/` are validation-only and must not become the production orchestrator CLI.
- Control logging via `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`); keep verbose structured logs configurable, never log API keys or secret-bearing prompts.
