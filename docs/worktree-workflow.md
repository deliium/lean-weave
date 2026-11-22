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

## V2 workflow context opt-in

The trusted operator config may select the context mode for feature execution:

```yaml
context:
  workflow_mode: v2   # v1 (default) | v2 | v2-with-v1-fallback
```

- `v1` keeps the original `ContextProvider.build` behavior and performs no V2
  cache, index, map, or packing construction.
- `v2` supplies each stage with its own scoped evidence. Planning gets approved
  paths and compact map metadata without source bodies. Implement and retry get
  the active task's complete units, direct dependencies, and counted raw-byte
  edit revisions. Verification and diagnosis stay deterministic.
- `v2-with-v1-fallback` only falls back to V1 rendering for an implement attempt
  whose sole safe pre-send reason is `no_relevant_source`. Budget, authority,
  stale, incomplete, guard, and provider failures never fall back.

The selected mode is frozen into the feature authority digest. Resuming a V2
feature with a different mode, packing/retrieval/cache setting, read policy, or
model cap is refused as authority drift before another dispatch. **Rollback is
choosing `v1` for new features**; do not swap the mode on an active V2 feature.

Expansion is only through the existing `read_range` / `read_symbol` tools.
Accepted fragments appear in the next freshly assembled source block; duplicate
or fully covered requests return `no_change`. Confirmed owned edits rebuild
current source without resetting expansion, source, call, or token caps. Stale
or unproven source is refused rather than replaying an old block.

V2 context is checkout-local. It does not enable peer cache reuse, implicit
index/cache maintenance, new write authority, or background work. Missing or
disabled index/cache services degrade only the optimization.

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

## Usage statistics

`wl stats` and `wl stats feature <id>` read the same routing record, checkpoint
and journal used by `status` / `result`; they never provision, diff, verify,
construct a provider or mutate state. Checkout inventory enumerates routed
feature IDs in sorted order only. Archived features read the moved checkpoint
and journal under the same recorded state directory, so live and archived
totals match.

Reports separate workflow status from provider evidence: a `success` feature
can still have an unavailable or partial journal. A missing journal is
`unavailable`, not zero. Corrupt evidence under an unchanged route fails with a
stable typed error; inventory keeps valid rows and reports per-feature failures
with partial coverage. The default text output is human-readable; `--json`
emits a versioned machine view. Diagnostics go to stderr.

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

## Release validation

Paired V1/V2 workflow runs exercise review/approve, stale approval, bounded
repair, budget refusal, interruption/resume, uncertain effects and worktree
isolation through the existing CLI suites. Current verdict and evidence are in
[v2-readiness.md](v2-readiness.md). Resume rules are unchanged: a checkpoint
must continue with the same mode and authority, and uncertain effects are never
replayed.
