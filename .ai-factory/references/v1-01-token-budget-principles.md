# v1-01 Token Budget Principles (ContextProvider)

Logged: `[TokenPrinciples.define]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.

Feeds: ARCHITECTURE.md, ContextProvider contract (Tasks 8/13).

## Goals

Minimize total LLM tokens across plan → implement → verify → retry while preserving enough context for correct edits. Prefer **deterministic selection** over LLM-based retrieval in MVP 1.

## Stage-scoped prompts

| Stage | Allowed context classes | Excluded |
|-------|-------------------------|----------|
| plan | User request; DESCRIPTION digest; ARCHITECTURE boundaries; optional ROADMAP milestone line; empty or stub FeatureState | Skill trees; full chat; unrelated source trees; prior implement transcripts |
| implement | Active `ImplementationTask` only; allowlisted files (task Files + deterministic deps); FeaturePlan summary (goal, settings, task titles); prior task **digests** (status + 3–5 line outcome) | Other tasks’ full prompts/outputs; verify transcripts; full skill docs |
| retry | Failure `VerificationResult` digest; failing file excerpts (bounded); active task restatement | Prior successful-stage full LLM messages; unrelated failures |
| verify | Prefer **no LLM**; if used later, gate rubric + file list only | Plan/implement chat history |

Never load another stage’s raw LLM transcript into the current stage.

## Digests vs full files

- **Always digest:** DESCRIPTION, ARCHITECTURE, completed tasks, verification reports, skill methodology (use extracted templates from Task 3).
- **Full file only when:** path is on the stage allowlist **and** size ≤ per-file budget.
- **Truncate with markers:** if over budget, keep imports/signatures + targeted regions; record truncation in MetricsCollector (not in prompt secrets).

## Deterministic file selection (MVP)

1. Explicit paths from the active task `files` list.
2. Same-package imports one hop from those files (static parse; cap hop count).
3. Matching tests under `tests/` for those modules when Testing=yes.
4. Stop. No embedding search / agent Explore in MVP 1.

## Max context budgets (initial defaults)

Budgets are **soft caps** enforced by ContextProvider before LLMProvider calls. Tune via config later; values are architecture defaults:

| Budget | Default | Applies to |
|--------|---------|------------|
| `stage_prompt_tokens_max` | 6_000 | Assembled user+system prompt excluding tools schemas |
| `per_file_chars_max` | 12_000 | Single source file inline |
| `total_code_chars_max` | 40_000 | Sum of code excerpts in one call |
| `digest_chars_max` | 2_000 | Each artifact digest |
| `history_messages_max` | 0 | Chat history in FeatureState (must stay 0 for MVP) |
| `retry_excerpt_chars_max` | 8_000 | Combined failure excerpts |

MetricsCollector records estimated prompt/completion/reasoning tokens per call; orchestrator may abort a stage if cumulative feature tokens exceed a future hard cap (open risk).

## Never enter LLM context

- API keys, `.env` contents, credentials, private keys
- Full `.cursor/skills/` or `.opencode/skills/` trees (≈1.3 MB)
- Entire chat / tool transcripts as FeatureState
- Duplicate OpenCode + Cursor skill copies
- Binary / lockfile blobs (`uv.lock` wholesale), `.venv/`
- Secrets redacted from logs must also be absent from persisted prompts

## Include / exclude decision log (DEBUG-oriented)

| Candidate | Decision | Reason |
|-----------|----------|--------|
| aif-plan SKILL.md wholesale | exclude | Token bloat; use template fragment |
| FeaturePlan JSON | include (compact) | Structured control plane |
| Active task files | include (budgeted) | Needed to edit |
| Completed task full LLM output | exclude | Digest only |
| pytest failure tail | include on retry (budgeted) | Actionable |
| DEEPSEEK_API_KEY | exclude | Secret |
| README / docs pages | digest if cited | Rarely need full |

## INFO summary

Budgets/principles finalized: stage isolation, digest-first artifacts, deterministic allowlists, zero chat history in state, hard bans on secrets and skill trees.
