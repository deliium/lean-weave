# Implementation Plan: Safe Code Editing and Verification Tools

Plan ID: v1-05-code-tools
Branch: none created (`main` left unchanged)
Created: 2026-11-19

## Original Request

Plan ID: v1-05-code-tools
Plan Title: Safe Code Editing and Verification Tools

Dependencies:
- v1-01-architecture
- v1-02-project-foundation
- v1-04-context-builder

Goal:
Implement a small, secure set of deterministic tools for reading, modifying, and verifying source code.

Requirements:
1. Implement read_symbol.
2. Implement read_range.
3. Implement search_symbols.
4. Implement apply_patch with explicit validation.
5. Implement run_checks using configured commands.
6. Implement Git diff inspection.
7. Define strict input and output schemas for each tool.
8. Support configurable file access restrictions.
9. Prevent path traversal and modifications outside the workspace.
10. Preserve file encoding and newline conventions where possible.
11. Detect patch conflicts and outdated source assumptions.
12. Support command timeouts and output size limits.
13. Return compact, structured tool results.
14. Return concise failure diagnostics suitable for LLM retries.
15. Add comprehensive tests covering success and failure cases.

Security:
- Do not expose unrestricted shell execution to LLMs.
- Do not allow arbitrary commands from model output.
- Restrict verification commands to an approved configuration.
- Protect secrets and environment files.
- Reject unauthorized file modifications.
- Do not execute destructive Git operations automatically.

Token-efficiency requirements:
- Limit tool output to relevant information.
- Avoid returning complete test logs by default.
- Deduplicate repeated diagnostics.
- Support targeted source reading.
- Prefer structured error summaries over raw command output.

Constraints:
- No autonomous agent loop in this stage.
- No LangGraph workflow implementation yet.
- No worktree lifecycle management yet.
- Tools must work independently from LLM and CLI integrations.

Acceptance criteria:
- Tools can read and modify files safely.
- Invalid patches fail without corrupting files.
- Verification commands are controlled and bounded.
- Source and diagnostic outputs are compact.
- Security and tool tests pass.

Save the plan using the exact identifier:
v1-05-code-tools

## Settings

- Testing: yes
- Logging: verbose; honor `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`; emit metadata only
- Docs: yes; mandatory `/aif-docs` checkpoint
- Dependencies: [v1-01-architecture](v1-01-architecture.md), [v1-02-project-foundation](v1-02-project-foundation.md), [v1-04-context-builder](v1-04-context-builder.md)

## Roadmap Linkage

Milestone: "CodeExecutor + VerificationRunner"
Rationale: This plan delivers standalone code editing and check primitives; it does not by itself close the broader milestone or wire feature-state verification.

## Requirements Reconciliation

The original request defines scope. `.ai-factory/RULES.md` and the v1-01 component and isolation contracts constrain security. v1-02 supplies strict config and redacted logging; v1-04 supplies repository admission and AST analysis. The existing `CodeExecutor.run(ExecRequest)` accepts argv at a lower-level contract seam; `VerificationRunner.run(FeatureState)` returns workflow gates. Neither is the public tool API. This plan does not expose either contract to tool callers or wire `AppContext`, CLI, providers, workspace lifecycle, workflow state, or model tools. Do not reinterpret the roadmap as adding those dependencies.

| Supported combination | Source and decision | Verification |
|---|---|---|
| Explicit read or bounded search of eligible paths; denied, missing, ambiguous, unsupported, or over budget | Request 1–3, 7–9, 13–14; v1-04 repository admission. Explicit selection can narrow but never bypass policy. Reads are snapshot-based and never write. Report safe codes; return bounded candidate locations on symbol ambiguity, never guess. A search that could not inspect all eligible candidates is not a complete zero-hit result. | Exact and ambiguous Python symbols; line boundaries; bounded lexical search; denied explicit scope; unsafe ignore files and secret canaries. |
| Eligible existing text file + current revision + exact patch; stale revision, unauthorized target, invalid context or unsafe output | Request 4, 8–11; project rules. Successful mutation replaces one authorized file only; pre-commit rejection leaves original bytes intact. Report committed-but-durability-unconfirmed separately from rejection. No creation, deletion, rename, mode changes, fuzzy matching, or multi-file transactions in this version. | Stale revision after a successful read/write, conflict in last hunk, injected failures on both sides of replace, permission denial, byte-level encoding/newline assertions. |
| Approved check IDs + trusted execution capability; unknown IDs, no checks, unavailable isolation, nonzero exit, timeout, or capture limit | Request 5, 7, 12–14; v1-01 `v1-01-isolation-security.md` § CodeExecutor sandbox. Approval binds argv, not edited code. Workspace-code execution requires offline isolation and a read-only workspace view; never let requests choose a weaker backend. Preflight the entire batch before spawning. No checks and blocked checks are not passes. | A valid check followed by an invalid or blocked check starts neither; test ordered results, success, failure, timeout cleanup, capture limits, and secret-free output. |
| Safe Git repository + staged or unstaged mode; Git absent, unsupported, or base missing | Request 6, 8–9, 12–14; v1-01 isolation § Git isolation combinations. Only bounded, read-only inspection of approved modified paths in the selected view. On unborn HEAD, staged inspection returns `missing_baseline`; unstaged inspection still uses the index when usable. No arbitrary revisions, flags or pathspecs, or Git mutations. No Git invocation when project Git metadata is absent or an explicit selection is denied. | Temp Git repo with diverging index/worktree, unborn HEAD, clean and incomplete results, and a denied explicit path. Confirm unchanged index and worktree. |
| Real repository artifacts | `src/lean_weave/app.py` and the dependency plans are evidence for code shape. Use the tracked app module in a temporary workspace through the standalone API. | Search/read `create_context_provider`, apply an exact harmless edit based on its revision, inspect it in Git, then parse the edited source with an approved offline check. Do not modify the checkout. |

## Commit Plan

- **Commit 1** (after tasks 1–4): `feat: add bounded workspace reading and atomic patching`
- **Commit 2** (after tasks 5–8): `feat: add approved checks and read-only Git diff inspection`

## Tasks

### Phase 1: Tool contracts and safe source access

- [x] Task 1: Define standalone APIs, strict schemas, and trusted configuration.
  - Add `src/lean_weave/executor/{__init__,models}.py` and `src/lean_weave/verify/{__init__,models}.py`. Use strict, extra-forbidden Pydantic requests, discriminated success/failure results, bounded strings/lists and validated cross-field invariants. Use one request/result pair for each of `read_range`, `read_symbol`, `search_symbols`, `apply_patch`, `run_checks`, and `inspect_git_diff`. Use project-relative POSIX paths and 1-based inclusive source lines; use consistent `revision` (SHA-256 of original file bytes) across reads and patches, distinct from context-rendered digests. Return stable error/status codes with canned retry guidance; validation errors must never serialize rejected input or raw exception text.
  - Extend `src/lean_weave/config/models.py` with optional tooling policy: read/write allowlists, additive excludes, bounded scan/file/result limits, and limits on ranges, hits, patch bytes/hunks, checks, runtime, and capture. No writes or commands enabled by default; default read scope cannot exceed context eligibility. Writes also require read authorization. Use existing config loading; validate an immutable, trusted approval snapshot before tool use. In `src/lean_weave/cli/commands/config.py` keep config display safe by omitting command argv and policy values that could expose secrets; counts and numeric bounds are sufficient. Do not add tool CLI commands.
  - Keep YAML configuration as data, not authority: the standalone constructor requires a trusted workspace root and separate explicit approval for any write scope or check registry. Snapshot approved fields immutably; mutable config objects or later on-disk changes cannot widen them. Resolve state and protected config paths against the explicit root with provenance for externally supplied config; do not call cwd-based `resolve_state_dir()` from standalone tools. Reject protected paths whose resolution makes the workspace boundary ambiguous.
  - **Evidence:** Requests 7–9, 12–14 and all reconciliation rows; `tests/config/` and `tests/executor/test_models.py` for defaults, old config, missing/extra/wrongly typed fields, finite limits, strict JSON round trips, canary-safe error serialization, and valid YAML that never authorizes writes or spawn by itself. Exercise construction from a different cwd and policy mutation after approval. **Logging:** DEBUG effective numeric limits and validation outcome codes; no payloads.

- [x] Task 2: Build one workspace authorization and snapshot layer (depends on 1).
  - Add `src/lean_weave/executor/workspace.py`. Reuse or extract policy, ignore matching, credential checks and snapshot primitives from `src/lean_weave/context/{policy,repository}.py` without changing context behavior. Inventory membership and lexical normalization are not authorization; re-admit fresh content per operation. Enforce bounds while traversing and reading, before unbounded sorting or accumulating matches. Hard-deny VCS metadata, state/config and skill directories, configured state and approval files, secrets/env/key files, generated/dependency trees and untrusted nested repos. Reject malformed paths, path aliases, absolute/traversing paths, symlinks in any component, non-regular files, and write targets with multiple hard links; refuse writes where the required safe filesystem primitives are unavailable.
  - Pin the explicit trusted workspace root; resolve against root-anchored directory handles with no-follow operations for mutation, not a string-prefix check followed by a pathname open. Snapshot bytes and file identity once per operation within budgets. Where decoding is supported, retain encoding/BOM, physical line terminators and final-newline state; for Python honor its encoding declaration, otherwise use strict UTF-8 with optional BOM. Do not treat the credential filter as a universal secret detector.
  - Treat truly absent `.gitignore` files as normal. Unlike the preview fallback in `Repository._load_ignore_file`, an applicable symlinked, unreadable, malformed, or over-budget ignore file is indeterminate policy: deny explicit operations for its subtree and mark discovery incomplete without disclosing omitted paths. Count attempted reads, including rejects, against the shared scan budget.
  - **Evidence:** Request 8–11 and read/patch/Git rows; `tests/executor/test_workspace.py` exercises read vs write scope, nested ignore and additive deny precedence, config aliases, symlinks, hard-link writes, special files, unsupported encodings and cap boundaries. Run v1-04 context tests for reuse regressions. **Logging:** DEBUG admission counts and outcome codes; WARN for incomplete discovery only, with no denied path or content.

- [x] Task 3: Implement targeted reads and Python symbol search (depends on 2).
  - Add `src/lean_weave/executor/source.py`. `read_range` takes `path`, `start_line`, `end_line`; `read_symbol` takes `path` and a qualified or unambiguous simple name; `search_symbols` takes a bounded literal name query and optional authorized path scope and tighter hit limit. Reuse `src/lean_weave/context/python_index.py` for classes, functions and methods without importing repository modules. Search uses exact-before-prefix ordering with stable path/line tie-breaks and returns qualified name, kind, path and source span, not bodies. Reads return just the requested range or the whole selected symbol (including decorators when present), with revision and provenance. On an oversized complete unit, offer a narrower range rather than silently cutting code.
  - Reject denied or indeterminate paths in an explicit search scope instead of silently widening the scope. For discovery, return admitted hits plus a completeness flag and bounded omission reasons, with no omitted path names.
  - **Evidence:** Request 1–3, 13–15 and read row; `tests/executor/test_source.py` covers decorated and nested symbols, duplicate names, syntax errors, offsets with multibyte text, no hits, stable ordering, and bounded complete output. Bound AST work and handle parser/visitor resource-limit failures as typed results or explicit search omissions. **Logging:** DEBUG operation, candidate/hit counts and limit outcomes; never log query text, snippets or signatures.

- [x] Task 4: Implement exact single-file patch validation and commit (depends on 2–3).
  - Add `src/lean_weave/executor/patch.py`. Accept `path`, mandatory `expected_revision` and a bounded single-file unified diff. Require both headers to name the same authorized path, validate ordered non-overlapping hunk spans/counts, and apply exact context/removals against the snapshot with no fuzz or offset guessing. Reject unsupported directives, malformed no-final-newline markers, and binary edits. Build the entire postimage before any replacement; enforce encoding, file/output bounds and credential policy on it too. Preserve the original BOM, encoding and unchanged terminators; use the unambiguous local convention for inserted lines or reject ambiguous edits instead of normalizing the whole file.
  - Serialize cooperative edits and recheck identity, authorization and byte revision immediately before replacement. Stage in the same authorized directory with an exclusive temporary file, preserve ordinary permission bits, flush before atomic replace, and clean up temporary files on pre-commit failure. Return new revision and changed line counts or a conflict code with refresh guidance, not raw patch lines. Document that this protects against stale inputs and atomic partial writes, not arbitrary concurrent filesystem writers; require a controlled workspace and no untrusted concurrent directory mutation. Distinguish a committed change from a pre-commit failure if post-commit durability cannot be confirmed.
  - Match decoded logical line content exactly (no whitespace normalization); accept LF patch transport for CRLF files without changing retained terminators. Validate the no-final-newline marker against the respective old or new EOF and reject contradictory placement. Replacement is the commit point: a subsequent durability error returns `committed` with the new revision and a durability warning, not a retryable rejection. Exercise stale retry with a revision obtained via Task 3.
  - **Evidence:** Request 4, 8–11, 14–15 and patch row; `tests/executor/test_patch.py` checks two-hunk success and last-hunk failure, stale revision outside hunks, cancellation/I/O failure before commit, replacement or parent identity changes, file modes, UTF-8/BOM and declared encoding, CRLF, and missing final newline. Assert byte equality on every pre-commit rejection. **Logging:** DEBUG validation stage and safe reason, INFO committed line count, WARN recognized conflicts; never log diff text.

### Phase 2: Controlled verification and diff inspection

- [x] Task 5: Define check approval and bounded subprocess execution (depends on 1–2).
  - Add `src/lean_weave/verify/process.py` and strict check definitions in `src/lean_weave/config/models.py`: stable IDs mapped by trusted configuration to fixed argv, approved executable, timeout, and output cap. Approval is explicit, comes from the trusted caller (not merely from finding a config in a workspace), and cannot be reloaded or widened by tool requests. Use fixed workspace cwd, a minimal explicit environment without inherited credentials or executable-search overrides, closed stdin, argv only, and no `shell=True`. No runtime substitution or request-controlled arguments.
  - Use a bounded shared capture runner for stdout/stderr, enforce per-command and aggregate deadlines/byte caps, and terminate and reap the process group on timeout or output overflow. A direct local backend may run only operator-approved, known-offline read-only checks that treat workspace files as data and do not load workspace code, dynamic plugins or project config. Bind executable and helper references outside editable workspace paths; do not resolve them through a request-supplied environment or workspace search path. For commands that run workspace code, require a trusted injected backend that enforces offline execution, denies protected data, and gives the command a read-only workspace view with separate controlled scratch space if needed. Report `isolation_unavailable` otherwise. No new sandbox infrastructure or network-enabled mode here. Document limitations of process-group cleanup and never fall back to unsandboxed execution.
  - **Evidence:** Request 5, 12, security clauses and checks row; `tests/verify/test_process.py` uses offline helpers for timeout/size caps, environment canaries, cwd, descendant cleanup and validation before spawn; denial tests for absent isolation. **Logging:** DEBUG backend type, duration and byte counts, WARN limit/isolation outcome; no argv, environment, or process output.

- [x] Task 6: Run approved check batches and return compact diagnostics (depends on 5).
  - Add `src/lean_weave/verify/checks.py`. `run_checks` accepts a bounded ordered list of check IDs (or omission for the approved defaults); reject empty explicit lists, duplicates, and unknown IDs before spawning. Report `no_checks` for an empty default registry. Run sequentially and report per-check status (`passed`, `failed`, `timed_out`, `output_limited`, `blocked`, or `not_run`), exit status when available, and a capped set of stable diagnostics. An incomplete batch is not a pass. No raw logs in public results; emit a fixed summary for unrecognized output. For recognized diagnostic formats retain only approved source locations, diagnostic codes, and safe summaries; sanitize controls, redact secrets before emission, and deduplicate within the result by normalized code/location/summary, keeping first occurrence and reporting omissions.
  - Before starting any check, preflight authorization and backend capability for every selected entry. If any entry is blocked, report the block and mark otherwise runnable entries `not_run`. After execution starts, retain completed statuses and mark unstarted entries `not_run` when the aggregate budget expires. Diagnostic summaries come from fixed templates keyed by recognized codes; do not interpolate free-form child text. Validate and authorize optional source locations separately from the diagnostic text.
  - **Evidence:** Request 5, 12–15 and checks row; `tests/verify/test_checks.py` covers ordering, pre-validation, mixed results, deadlines across a batch, duplicate failures and hidden canaries in multiline, truncated and unknown output. **Logging:** INFO batch outcome and counts, DEBUG diagnostic counts, WARN blocked or resource-limited batches; no raw stdout/stderr.

- [x] Task 7: Implement bounded staged and unstaged Git diff inspection (depends on 2, 5).
  - Add `src/lean_weave/executor/git_diff.py`, following the isolated subprocess conventions in `src/lean_weave/context/git_metadata.py` but not its recency semantics. `inspect_git_diff` takes a fixed mode and optional exact eligible paths. Return deterministic, capped path/status entries for the selected view (staged = index vs. HEAD, unstaged = worktree vs. index); no untracked-file scan. Keep the first version metadata-only: no patch bodies or historical blob content in results. Disable rename detection to avoid cross-policy path disclosure; classify separate additions/deletions. Authorize record paths before returning them; handle removed files without opening them, and apply content admission to available working snapshots. Do not expose omitted filenames.
  - Use fixed machine-readable, NUL-delimited read-only Git commands, no external diff/text conversion, helpers, optional locks, or lazy fetch, no caller-provided flags/revisions, and no inherited Git overrides. Preflight a repository rooted in the workspace using the existing config safety checks; return typed unavailable/unsupported for no Git, unsafe or indirect metadata, or no supported comparison baseline, and do not search parent repos. Where no working snapshot exists, return only policy-approved path metadata, not a claim about historical content. Share subprocess budgets and expose incomplete results explicitly; do not publish partially parsed records or misreport failed inspection as clean.
  - Validate explicit path scope before any Git invocation, then filter parsed results by exact identity without rewriting path characters. Reject unsafe metadata rather than treating it as absent; distinguish a missing `.git` entry from a dangling link. Return `clean` only for an empty successful comparison, `incomplete` for an interrupted capture or authorization scan, and `unavailable` for missing or unsupported Git metadata/baseline. For a successful comparison with changes but no visible authorized entries, return an empty filtered view without disclosing omitted paths or counts. Do not reuse the context preview’s permissive parser or empty-output fallback.
  - **Evidence:** Requests 6, 8–9, 12–15 and Git row; `tests/executor/test_git_diff.py` covers staged/unstaged separation, deleted and restricted paths, whitespace and unusual names, missing Git/baseline, output bounds, and unchanged index/worktree. **Logging:** DEBUG mode, counts and safe status; WARN on incomplete or unsupported views; no raw Git output.

- [x] Task 8: Verify the public API end to end and document its boundaries (depends on 3–4, 6–7).
  - Add `tests/executor/test_code_tools_integration.py` using a temporary copy of tracked `src/lean_weave/app.py`, a temporary Git index, and an explicitly approved offline parser helper outside the editable workspace. Search and read a real symbol, patch with its exact revision, verify stale retry guidance, inspect the correct Git view, run the parser check, and assert no provider or CLI setup and no changes outside authorized files. Assert encoded output sizes for result envelopes. With the project environment already synced, run `uv run --offline --no-sync pytest tests/config tests/context tests/executor tests/verify` followed by `uv run --offline --no-sync pytest --ignore=tests/spikes`. Tests must not require network; the pytest socket guard alone is not evidence of child-process isolation.
  - At the `/aif-docs` checkpoint, update `docs/architecture.md`, keep the config example in `docs/cli.md` in sync, and add/link a standalone tooling guide under `docs/` with schema examples, configuration provenance, authorization, patch limitations, Git views, process isolation and safe diagnostics. Do not advertise a CLI command, cross-platform security guarantees, or milestone completion not delivered here.
  - **Evidence:** Request 15, all acceptance criteria, and real-artifact row. **Logging:** Test that DEBUG and failure paths expose neither secrets nor raw inputs; document supported log levels and safe fields.
