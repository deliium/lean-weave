# v1-01 DeepSeek API Validation

Logged: `[DeepSeekProbe.case]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.  
Sources: https://api-docs.deepseek.com/guides/thinking_mode , tool_calls, json_mode, quick_start (fetched 2026-11-08).  
Probe: `spikes/deepseek_probe/` · Tests: `tests/spikes/test_deepseek_probe.py`.

## Mode run

| Mode | Trigger | Result |
|------|---------|--------|
| offline | `DEEPSEEK_API_KEY` absent (this session) | Fixture shape assertions **pass** |
| live | `DEEPSEEK_API_KEY` set | Exercises non-thinking tools, thinking+tools round-trip, JSON object |

## Capability matrix

| Capability | Thinking | Tools | Behavior / caveat |
|------------|----------|-------|-------------------|
| Tool calling | off | present | Supported (`thinking.type=disabled`) |
| Tool calling | on | present | Supported; **must echo** every prior `reasoning_content` or API **400** |
| Multi-turn chat | on | absent | `reasoning_content` **ignored** if sent; no echo required |
| `response_format=json_object` | either | usually off | Prompt must include word `json` + example; blank/whitespace `content` possible if parse fails |
| Beta `strict` tool schemas | on/off | present | Requires `base_url=https://api.deepseek.com/beta` and `function.strict=true`; schema must list all props in `required`, `additionalProperties:false` |
| Usage reporting | — | — | `prompt_tokens`, `completion_tokens`, `completion_tokens_details.reasoning_tokens`, `prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens` |
| Default thinking | — | — | Docs: thinking **enabled by default**, default effort `high` |
| Models | — | — | `deepseek-flash` (V4.1 Flash), `deepseek-v4-pro` |

## Doc / API notes for LLMProvider

1. OpenAI-compatible client with `base_url=https://api.deepseek.com`.
2. Pass `thinking` via `extra_body` in OpenAI Python SDK.
3. When `tools` is set, assistant message replay **must** include `reasoning_content` (append `response.choices[0].message` wholesale).
4. Do not rely on `temperature` / penalties in thinking mode (no-ops).
5. Prefer non-thinking for cheap structured planning JSON; use thinking+tools only when tool loops need CoT continuity.
6. MetricsCollector should record `reasoning_tokens` separately when present — they inflate completion cost.
7. Live probe not executed in this architecture session (no key); offline fixtures encode required shapes.

## MVP implications

- LLMProvider API must expose: `thinking: on|off`, `tools`, `response_format`, and return structured usage including reasoning/cache hits.
- ContextProvider/orchestrator must store assistant `reasoning_content` **only for in-flight tool loops**, not as FeatureState chat history.
- Strict beta tools optional for MVP; document as enhancement.
