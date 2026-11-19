[← Architecture](architecture.md) · [Back to README](../README.md) · [CLI Reference →](cli.md)

# Standalone code tools

`executor` and `verify` are a small, offline API for reading, patching, and checking files in one explicit workspace. They do not start a workflow, call a model, register CLI commands, or persist an index. Loading YAML never grants a write or a process spawn.

There is no `wl` or `lean-weave` command for these tools. The CodeExecutor and VerificationRunner workflow contracts stay unwired.

## Approval

Construct a snapshot with an explicit workspace root. Relative paths in the snapshot are resolved from that root, not the process working directory.

```python
from pathlib import Path

from lean_weave.config import AppConfig
from lean_weave.executor import approve_tools, read_symbol

root = Path("/path/to/workspace")
approval = approve_tools(root, AppConfig())
result = read_symbol(approval, {"path": "src/app.py", "name": "build"})
```

`allow_writes=True` copies `tools.write_allow` into the snapshot. `allow_checks=True` copies the check registry. Without those flags the lists in YAML stay inert. Later edits to the `AppConfig` object do not widen a snapshot that was already built.

`config show` prints numeric tool limits plus `read_allow_count`, `write_allow_count`, `exclude_count`, and `check_count`. It does not print allowlists, exclude patterns, check argv, or executable paths.

Protected paths include the configured state directory and its `config.yaml`. A protected path that resolves through a symlink, or that escapes the workspace, is rejected when the snapshot is built.

### Configuration provenance

Pass the policy source as `config_path` to record where the approval came from. The value is available on the snapshot as `approval.config_provenance`:

| Value | Meaning |
|-------|---------|
| `workspace` | The path resolved inside the explicit root. It is also protected from reads and writes. |
| `external` | An absolute path outside the root. It is recorded as provenance but is not part of the workspace. |
| `none` | No config path was supplied; defaults or a caller-built `AppConfig` were used. |

Paths are resolved from the explicit root, not the process working directory. The effective limits are copied into a frozen snapshot, so editing YAML or the source `AppConfig` after approval does not widen it.

## Request and result shapes

All requests are strict, extra fields are rejected, and failures return a stable status plus canned retry guidance. Rejected input is never echoed. These examples are illustrative; the Pydantic models are authoritative.

```json
{"path": "src/app.py", "start_line": 10, "end_line": 25}
{"ok": true, "status": "ok", "guidance": "", "path": "src/app.py",
 "start_line": 10, "end_line": 25, "text": "...", "revision": "sha256:...",
 "encoding": "utf-8", "newline": "lf"}
```

```json
{"query": "create_context_provider", "paths": [], "hit_limit": 10}
{"ok": true, "status": "ok", "guidance": "", "complete": true,
 "truncated": false, "omissions": [],
 "hits": [{"path": "src/app.py", "qualified_name": "app.create_context_provider",
           "kind": "function", "start_line": 10, "end_line": 25}]}
```

```json
{"path": "src/app.py", "expected_revision": "sha256:...",
 "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -10,1 +10,1 @@\n-old\n+new\n"}
{"ok": true, "status": "committed", "guidance": "...", "revision": "sha256:...",
 "lines_added": 1, "lines_removed": 1, "durability": "confirmed"}
```

```json
{"mode": "staged", "paths": []}
{"ok": true, "status": "changes", "guidance": "...", "truncated": false,
 "entries": [{"path": "src/app.py", "status": "M"}]}
```

```json
{"check_ids": ["parse"]}
{"ok": true, "status": "passed", "guidance": "...",
 "items": [{"check_id": "parse", "status": "passed", "exit_code": 0,
            "diagnostics": [], "diagnostic_omissions": 0}]}
```

## Reads and search

| Request | Result |
|---------|--------|
| `read_range` | `path`, inclusive 1-based lines, original terminators, byte `revision` |
| `read_symbol` | one class, function, or method, including decorators |
| `search_symbols` | qualified name, kind, path, and span; no source bodies |

`revision` is the SHA-256 of the original file bytes (`sha256:` plus 64 hex digits). It is not the context digest of normalized text.

An explicit search path that is denied or whose ignore policy is indeterminate fails the whole request. Discovery that cannot inspect every candidate, including oversized, undecodable, or unreadable files, returns `incomplete` rather than a definitive empty hit list. An oversized symbol returns `oversized` with the line span and no truncated body.

Hit lists and ambiguous-symbol candidate lists are capped by `tools.result_chars_max`; a trimmed list sets `truncated=true`.

The credential filter is a high-confidence pattern check, not a proof that a file is free of secrets.

## Patches

`apply_patch` accepts one existing authorized file, the current byte `revision`, and one unified diff.

- Both headers must name that same path. Creation, deletion, rename, mode changes, and multi-file patches are rejected.
- Hunks must be ordered and non-overlapping. Context and removals must match decoded line text exactly. There is no fuzz and no offset search.
- The full postimage is checked for size, encoding, and credential patterns before any replacement.
- Original BOM, encoding, and unchanged line terminators are kept. Inserted lines use the file's one terminator convention. Mixed terminators reject an insertion.
- LF patch text can update a CRLF file without converting the file to LF.
- A failed check before replacement leaves the original bytes in place.
- Inserting after a final line that has no newline is rejected unless the patch re-adds that line with the matching `\ No newline at end of file` marker. The tool never concatenates two logical lines.
- Ordinary permission bits are restored on the staged file; the process umask does not narrow them.
- After replacement, a failed directory flush returns `committed` with `durability="unconfirmed"` and the new revision. That is not a retryable rejection.

The replacement is atomic for one file on a single filesystem. It does not protect against other processes mutating the directory at the same time. Use a workspace you control.

A conflict or a stale revision returns a canned retry hint and no patch text. Re-read the file and retry with the new revision.

## Git views

`inspect_git_diff` is read-only and metadata-only. `mode` is `staged` (index versus HEAD) or `unstaged` (worktree versus index). Untracked files are not scanned. Rename detection is off, so a rename is not reported as a pair of paths.

| Status | Meaning |
|--------|---------|
| `clean` | The selected comparison had no changes |
| `changes` | At least one authorized path changed |
| `filtered` | Changes existed, but none were authorized to show. No omitted names or counts |
| `incomplete` | Capture or authorization was interrupted. Not a clean tree |
| `unavailable` | `.git` is missing, a symlink, a gitfile, or otherwise unsupported |
| `missing_baseline` | Staged view with no HEAD. Unstaged view can still use the index |

A missing `.git` does not run Git and does not search a parent repository. A dangling `.git` symlink is `unavailable`, not absent. Deleted paths are not opened. Available worktree files still pass the content policy; a credential match drops the path, and an exhausted scan budget returns `incomplete`.

Entry lists are capped by `tools.result_chars_max`; a trimmed list keeps status `changes` and sets `truncated=true`.

## Checks

`run_checks` runs only ids present on an approval that was built with `allow_checks=True`. Omitted ids use that registry in order. An empty registry is `no_checks`, which is not a pass. An empty list, a duplicate, or an unknown id fails before any process starts.

Each check has a fixed argv, an absolute executable, a timeout, and an output cap. Requests cannot add arguments. The direct backend is only for checks that do not load workspace code (`runs_workspace_code=false`). The executable and any absolute helper path must sit outside the workspace. The child environment is a fixed minimal map: inherited credentials, `PYTHONPATH`, and the parent `PATH` are not passed.

A check with `runs_workspace_code=true` needs a caller-supplied backend that enforces offline execution and a read-only view of the workspace. If that backend is absent the batch is `blocked` and nothing is spawned. This package does not add a sandbox, and it does not fall back to an unsandboxed run.

On timeout or output overflow the process group is killed. A descendant that has already left the session can outlive that signal. Completed checks keep their status; checks that never started are `not_run`.

Diagnostics keep a code, a fixed summary, and an authorized path and line when the path is readable under the same policy. Child text, denied paths, and secrets are not copied. Repeated code/location/summary rows collapse to the first one.

## Logging

Levels follow `LEAN_WEAVE_LOG_LEVEL`, then `LOG_LEVEL`. Tool logs record operation names, counts, and outcome codes. They do not record queries, source text, diffs, argv, environments, or process output.

## See also

- [Architecture](architecture.md)
- [CLI Reference](cli.md) — configuration display; no tool command
- [Context retrieval](architecture.md#context-retrieval) — the read policy these tools reuse
