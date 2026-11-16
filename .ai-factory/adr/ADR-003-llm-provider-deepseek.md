# ADR-003: LLM Provider — DeepSeek

- Status: Accepted
- Date: 2026-11-08

## Context

Pipeline targets DeepSeek OpenAI-compatible API. Docs validated for thinking mode, tool calling, JSON mode, usage fields (offline fixtures; live when key set).

## Decision

- Single provider in MVP: DeepSeek via OpenAI SDK (`base_url=https://api.deepseek.com`).
- LLMProvider must support thinking on/off, tools, `reasoning_content` echo rules when tools present, `response_format=json_object`, and expose usage including `reasoning_tokens` / cache hits.
- Default model: `deepseek-flash`; optional `deepseek-v4-pro`.
- Prefer non-thinking for cheap structured plan/JSON; thinking+tools only when tool loops need CoT continuity.
- Strict beta tool schemas optional (`/beta`); not required for MVP gate.

## Consequences

- MetricsCollector can attribute reasoning token cost.
- Tool-loop buffers are ephemeral; not FeatureState chat history.
- Live API behavior remains partially open until a keyed probe run (see risk register).
