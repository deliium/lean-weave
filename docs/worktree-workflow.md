[← Getting Started](getting-started.md) · [Back to README](../README.md)

# Worktree Feature Workflow

Public feature commands run inside an isolated Git worktree. The origin
checkout is never merged, reset, or cleaned by Lean Weave.

## Trusted operator config

Start and nonterminal resume require an explicit root flag:

```bash
uv run wl --config /secure/operator.yaml feature "add healthz" --auto
uv run wl --config /secure/operator.yaml feature "add healthz" --review
```

The config path must resolve **outside** the source checkout, the Git
administration directory, and the target worktree. Repository-checked-in
`.lean-weave/config.yaml` cannot authorize public feature execution. The config
is loaded once from the invocation context; commands never `chdir` into the
worktree.

Checks listed in the operator config are caller-attested data-only commands
when `runs_workspace_code` is false. Interpreters or test runners that load
repository code still need an injected isolation backend; a worktree is not a
process sandbox.

## Modes

- `--auto` — plan, implement, and verify without an intermediate pause.
- `--review` — persist the validated plan and pause before any write. The
  command prints the plan tasks and an exact approval command:

```bash
uv run wl --config /secure/operator.yaml resume <feature-id> --approve <token>
```

The token is bound to the canonical plan, feature ID, worktree identity, pinned
HEAD, frozen policy, approved write paths, **check definitions** (executable,
argv, limits, data-only flag), effective workflow limits, `workflow.state_dir`,
and a fingerprint of the approved files. Any edit to an approved file after the
pause makes the approval stale (`stale_baseline`). Same-ID check argv drift,
limit drift, or `state_dir` drift refuses resume before another provider
dispatch (`authority_drift` / `state_dir_drift`). Start a new feature instead of
widening an old gate.

A linked worktree isolates Git content; it is **not** an OS execution sandbox.
Public acceptance checks must be trusted external data-only commands. There is
no public workspace-code isolation backend in MVP 1.

## Lifecycle

```bash
uv run wl status <feature-id>
uv run wl result <feature-id>
uv run wl cancel <feature-id>
uv run wl worktree inspect <feature-id>
uv run wl worktree cleanup <feature-id> --confirm
```

Status, result, cancel, inspect, and cleanup do not require execution config,
API keys, or a provider. They read routing records and the frozen checkpoint
location only.

`result` reports the feature ID, workflow status and reason, branch and path,
bounded staged/unstaged/untracked change summary, modified paths, verification
summary, LLM usage, and a deterministic next action. It never prints raw Git
output, patches, or checkpoint contents.

Cancel is cooperative. Idle runs finalize under the worktree lease with a typed
`cancelled` result. Active runs receive a durable cancel request and stop at the
next safe boundary; the marker survives owner loss and a later `resume`
finalizes the cancellation instead of dispatching new effects. Pending uncertain
effects require inspection rather than being cleared.

## Failed runs and recovery

Interrupted runs retain plan, task outcomes, spend, and worktree edits. Resume
continues from the last validated safe stage and does not repeat completed LLM
stages. If a process dies after a confirmed edit but before its task outcome is
saved, or during an in-flight plan call, Lean Weave fails closed with
`manual_inspection` instead of replaying the effect. Inspect the worktree, keep
the edits you want, and start a new feature if the continuation is ambiguous.

## Cleanup and archival

Cleanup takes the same feature lease and refuses adopted, active, pending-effect,
stale, dirty, or unknown-state worktrees. Never-started managed worktrees are
eligible. For a terminal run, Lean Weave validates the checkpoint, moves only
recognized app-owned state files into an owner-only archive outside the
worktree, rechecks cleanliness, and removes the worktree without force. The
feature branch is never deleted. Removal leaves a routing tombstone; `status`
and `result` continue to read the archived terminal checkpoint while reporting
that the live diff is unavailable.

Cleanup with an incomplete cleanliness probe, an unknown file under the state
directory, or unprovable state reports manual recovery instead of guessing.

## State locations

- Routing records: private `lean-weave/` under the Git common directory
- Checkpoints and usage journal: per worktree under the configured state
  directory (default `.lean-weave/`)
- Managed worktree parent: owner-only sibling directory outside the source
  checkout
- Archived terminal state: owner-only `lean-weave/archive/<feature-id>/`

## Logging

Control verbosity with `LEAN_WEAVE_LOG_LEVEL` (fallback `LOG_LEVEL`). Logs use
structured reason codes and counts only — never plan prose, raw Git/check
output, credentials, or file contents.
