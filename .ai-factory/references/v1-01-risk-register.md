# v1-01 Technical Risk Register

| ID | Risk | Assumption status | Mitigation | Owner (later plan) |
|----|------|-------------------|------------|--------------------|
| R1 | Live DeepSeek behavior differs from docs/fixtures | **Open** (offline pass; no key this session) | Run live probe when `DEEPSEEK_API_KEY` available; adjust LLMProvider | LLMProvider impl plan |
| R2 | `reasoning_content` echo bugs → 400 in tool loops | Validated by docs; code path unproven live | Central helper appends full assistant message; spike covers offline shape | LLMProvider / orchestrator |
| R3 | JSON mode blank content | Validated (documented caveat) | Prompt includes `json`+example; retry on blank | LLMProvider |
| R4 | LangGraph complexity tax if prematurely adopted | Validated (deferred) | ADR-002 stick to minimal SM | Architecture owners |
| R5 | Token budgets too tight → poor edits | Open | Metrics + tunable config; start with documented defaults | ContextProvider plan |
| R6 | Prompt injection via repo files | Open (partial mitigation) | Allowlists; untrusted-data framing; argv-only executor | Isolation hardening |
| R7 | Subprocess escape / cwd breakout | Open | Path canonicalization; no shell=True | CodeExecutor plan |
| R8 | Git ops accidental destruction | Mitigated by policy | Forbid destructive commands; no push default | Git isolation |
| R9 | Skill methodology drift vs extracted templates | Open | Re-extract on major aif-* upgrades; do not load skills wholesale | Methodology maintenance |
| R10 | Deferred MCP/Docker/workers needed sooner | Deferred exclusion | Risk accepted for MVP 1; revisit roadmap | Product |

WARN: R1, R5, R6, R7 remain unvalidated in production code (architecture-only plan).
