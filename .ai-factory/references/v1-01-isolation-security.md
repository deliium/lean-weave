# v1-01 Context Isolation, Security, and Git Isolation

Logged: `[Isolation.define]` via `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`.

## Context isolation (plan / implement / retry)

| Stage | May load | Hard bans |
|-------|----------|-----------|
| plan | request; DESCRIPTION/ARCHITECTURE digests; empty state | implement/verify transcripts; skill trees; secrets |
| implement | active task; allowlisted files; plan summary; prior task digests | other-stage raw LLM messages; full verify logs; unrelated tasks’ file trees |
| retry | VerificationResult digest; bounded failing excerpts; active task | successful-stage full prompts; secrets; chat history |

ContextProvider must refuse cross-stage transcript leakage.

## Secret handling

- API keys **env-only** (`DEEPSEEK_API_KEY`); never write to StateStore or plan files.
- Never persist prompts that contain secrets; redact logs (`Authorization`, bearer tokens, `.env` lines).
- Spike/live probes must not print keys or secret-bearing full prompts.

## CodeExecutor sandbox (MVP 1)

- Local `subprocess` only; no Docker.
- `cwd` restricted to project root (or explicit worktree path).
- `allow_network=False` by default; tests must opt in explicitly.
- Capture stdout/stderr with size caps; timeouts required.
- No shell=`True` with unsanitized LLM strings — argv list only.

## Git isolation combinations

### (a) Git absent (current workspace)

- StateStore **only** under `.lean-weave/`.
- No git commands.
- MVP acceptance must pass in this mode.

### (b) Git present

- Optional feature branch / worktree policy aligned with AI Factory config (`feature/` prefix).
- Forbid destructive git ops in MVP (`reset --hard`, `push --force`, `clean -fdx`, etc.).
- **No push by default** (even if config later enables skip_push=false for human `/aif-commit`).
- StateStore still primary for FeatureState; git is isolation boundary for code edits, not for chat.

Both (a) and (b) appear in MVP 1 acceptance criteria.

## Prompt injection via repo files

- Threat: malicious README/code comments instructing the model to exfiltrate keys or skip verify.
- Mitigations: deterministic allowlists; stage system prompts that treat file content as untrusted data; never execute unsandboxed shell from model text; VerificationRunner ignores “skip tests” instructions in source comments.

## Residual risks → risk register

WARN: full OS sandboxing (seccomp/landlock) deferred; prompt-injection residual risk remains open for later hardening plans.
