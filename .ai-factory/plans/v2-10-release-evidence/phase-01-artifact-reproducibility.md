# Phase 1: Artifact Reproducibility

Plan: [index.md](index.md)  
Tasks: 1–2  
Depends on: `v2-08-e2e-optimization` and `v2-09-retrieval-generalization`

## Objective

Produce a repeatable build from a named commit and independently verify that the installed evaluation artifact is that build. Never upgrade a legacy wheel hash to verified evidence.

## Current-Code Evidence

| Path | Existing behavior |
|---|---|
| `pyproject.toml`, `uv.lock` | `uv_build` backend is a version range, not a pinned build tool; the runtime/dev lock is a separate input. The wheel contains `lean_weave.benchmark`. |
| `src/lean_weave/benchmark/conditions.py` | `source_revision`, `code_tree_dirty`, `source_tree_digest`, `dependency_lock_digest`, `installation_kind`, `wheel_sha256`, `release_binding`: Git calls are best-effort; source status is path-scoped, failed Git calls can look empty, and the `LEAN_WEAVE_BENCHMARK_WHEEL` path can point to an unrelated wheel. `SOURCE_ROOT` is derived from the module path, not an independently verified checkout. |
| `docs/v2-readiness.md` §5.2, §7, evidence inventory; commits `dcde3b42` → `91fc072` | The historical digest was reproduced by hashing the parent HEAD, binary diffs for the 11 modified in-scope tracked files, and sorted NUL-terminated names of the 12 added in-scope files as untracked. This identifies uncommitted v2-08 implementation as the cause, not the external evidence directory. The digest does not prove the untracked bytes or which tree produced the historical wheel. The stale “no wheel hash” bullet conflicts with §5.2 and the inventory; preserve both. |

## Files to Change

| Path | Action |
|---|---|
| `benchmarks/release_evidence.py` | Add a developer-only release driver (`build`, `verify-binding`, `freeze-run`; later phases add `offline`, `live`, `decide`). Do not add a product CLI or change wheel packaging defaults. |
| `src/lean_weave/benchmark/conditions.py` | Keep observational fields/readers compatible; make an environment wheel hash by itself ineligible as formal proof on new runs. |
| `tests/test_release_evidence.py` | Add isolated Git, wheel and binding verifiers; keep fixture wheels synthetic and local. |
| `tests/benchmarks/test_workflow_matrix.py` | Replace the ambient dirty-tree assumption in `test_release_binding_records_supplied_wheel_hash` with controlled source-status and wheel observations. |
| `docs/v2-release-evidence.md` | Add exact build, installation and revalidation procedure. |

## Task 1: Build twice from a clean commit

### Intent and implementation

1. In `benchmarks/release_evidence.py`, implement `build --repo REPO --commit FULL_OID --output NEW_DIR --build-inputs FILE`. The operator first resolves the full commit OID using an approved Git executable with external overrides and replacements disabled, then extracts the driver as a blob from that commit into a fresh private external directory. Record its blob ID and byte digest; launch `<pinned-python> -I -B <external>/release_evidence.py ...`. Keep the driver stdlib-only (including a canonical JSON digest verified against `conditions.canonical_digest` with test vectors); never bootstrap with `python -m` from the authoring tree or put source on the installed worker's module search path. Require the full canonical OID of an actual commit, not a tag, ref or abbreviation. The driver checks its own bytes against the committed blob and records commit/tree OIDs. The external build descriptor pins Python, Git, uv and `uv_build` identities, wheelhouse hashes, flags and reproducibility environment. No implicit tool downloads or resolver drift.
2. Create two independent detached `git worktree add --detach` worktrees at that commit with private external output, HOME/temp/cache directories. The authoring checkout is only the object source; do not copy working files or change branches. Verify exact Git top-level, `HEAD`, committed tree, and successful empty `git status --porcelain=v1 -z --untracked-files=all --ignored=matching` before and after build. Independently compare the worktree's tracked bytes, file types and executable bits against `git ls-tree` and raw blob bytes; reject unsupported links/gitlinks, sparse or hidden index entries, clean/smudge or line-ending transformation, and unexpected files (except the managed `.git` pointer). Disable inherited Git config/env overrides, hooks and external filters; if local repo attributes/config cannot be neutralized or produce a byte mismatch, stop. Include `README.md` in the identity check. Preserve user state; never clean or reset another worktree.
3. Execute pinned `uv --no-config --offline build --force-pep517 --wheel --python <pinned-python> --no-python-downloads --no-index --find-links <verified-wheelhouse> --build-constraints <exact-hashed-backend-requirement> --require-build-hashes --out-dir <fresh-dir> <worktree>` with fixed locale/timezone and `SOURCE_DATE_EPOCH` derived from the commit time. Record normalized argv, tool/platform versions, file digests for `pyproject.toml`, lockfile, constraint and wheelhouse receipts, exit statuses, clean status receipts and SHA-256 of each wheel. Accept exactly one expected project wheel per build and identical filename and bytes across both; do not normalize or rewrite discrepant wheels to pass. Preserve both artifacts and return a failed build record on mismatch. Keep all evidence/cache/environments external; cleanup only individually owned worktrees without force.

### Contract, failure and logging

`BuildProofV1` contains object IDs, before/after status success and cleanliness for each build, tool/build inputs and wheel filename/digests, matching result and bounded errors. Write one new evidence record even when a build fails; never overwrite a prior attempt. INFO: short commit and stage outcomes; DEBUG: normalized tool versions, argv and step timings; ERROR: reason codes (`commit_invalid`, `status_failed`, `source_tree_dirty`, `build_failed`, `wheel_mismatch`). Do not emit arbitrary status filenames, environment values, credentials or full host paths.

### Requirement evidence, tests and acceptance

Original Request A.1–6 and acceptance 1–2; `pyproject.toml` build system and historical §5.2. Tests in `tests/test_release_evidence.py`: real temporary Git commit and detached checkouts with dirty authoring tree ignored, altered driver, ref/short SHA, hidden index/tracked changes, transformed bytes despite clean status, untracked/ignored contamination, Git failure, missing lock/backend, multiple wheels and mismatched repeats. Use fixed fake build outputs now; after the final tooling checkpoint the operator runs the real double build. Run `uv run --locked --offline pytest tests/test_release_evidence.py`; require identical raw wheel digests and all four successful clean observations for real evidence. If the pinned toolchain cannot produce byte-equivalent wheels, block without changing packaging.

## Task 2: Bind evidence to the installed wheel

### Intent and implementation

1. Add `verify-binding --manifest FILE --repo REPO`, also called by later stages before/after running and at decision time. Define strict versioned JSON (`artifact-binding.json`): embedded or hashed `BuildProofV1`, wheel relative name and SHA-256, full commit/tree, build input/tool/lock digests. `artifact_id` is `conditions.canonical_digest` of the immutable payload excluding its own ID. Only allow private relative artifact paths beneath the evidence root; reject path/symlink escape, missing or duplicate keys, invalid hashes and unknown versions. Recheck actual wheel bytes, named Git commit and tree and committed lock/`pyproject.toml` bytes. Compare both stored build wheels and all required receipts, not only claimed digests.
2. Export hashed runtime requirements with pinned `uv --no-config --offline export --project <verified-worktree> --locked --no-default-groups --no-emit-project --format requirements-txt`; run with a cleared ambient `UV_*` environment and write externally. Sync a fresh external venv with `uv --no-config --offline pip sync --python <env-python> --no-index --find-links <verified-wheelhouse> --require-hashes <export>`, then install the verified local wheel using `uv --no-config --offline pip install --python <env-python> --no-deps <wheel>` after checking its bytes. Record export and dependency inventory digests. No editable install, dependency re-resolution or `PYTHONPATH`. From a fixed external cwd, use absolute installed CLI paths or `<env-python> -I -B <verified-external-worker-script>` (a committed exported blob). Compare installed `lean_weave` package bytes and metadata/resources against wheel contents/`RECORD`, rejecting extra executable files, editable installs and import shadowing. Revalidate before and after each installed run; source pytest is `source-regression`, never `installed-wheel`.
3. Add `freeze-run --manifest FILE --preregistration FILE --output NEW_DIR`, which writes `ReleaseRunV1` before any evidence execution. Compute `run_id` from canonical JSON of schema version, a fresh run nonce, artifact ID, policy digest, custodian key fingerprint/approval identity, and ordered stage expectations (tool, runtime mode, config, dataset/fixture commitment and schedule digests). Preserve the approved `run_id` outside the run directory in an operator-controlled record; phase commands and `decide` require this independent pin, not a value taken from the receipts being verified. `EvidenceEnvelopeV1` includes `run_id`, stage/schedule ID, artifact ID, commit, wheel hash, config, dataset, runner/runtime mode, output hashes, status and reason codes. Use fixed stage-relative paths, privately owned output roots, and exclusive publication after writing a complete record to a sibling temp file (no overwrite). Exact reruns need a new run ID and cannot reuse old receipts. Keep historical JSON readable and never assert eligibility from a wheel-path environment variable alone.

### Contract, failure and logging

Fail closed on source/wheel/lock/config/dataset/tool mismatch, missing receipt, incomplete binding, install drift, unknown schema or unverified phase. INFO: artifact ID, runtime mode, stage disposition; DEBUG: digest and field comparison names; WARN: deliberate historical exclusion; ERROR: `binding_incomplete`, `binding_mismatch`, `installed_wheel_unverified`. Never log raw config, fixture labels, credentials, or absolute paths in evidence.

### Requirement evidence, tests and acceptance

Original Request A.6–10, C.3, release gate; v2-08 §5.2's observational hash is not install/build attestation. Extend `tests/test_release_evidence.py` for wrong wheel, byte mutation, import shadowing, stale lock, cross-commit/config records, same-artifact cross-run swaps, changed freeze file, duplicate publication, path escape, source records mislabeled as installed and legacy reading. Run `uv run --locked --offline pytest tests/test_release_evidence.py`; after the final tooling checkpoint run `<pinned-python> -I -B <external>/release_evidence.py verify-binding --manifest <artifact-binding.json> --repo <repo>` on the real double build.

## Phase Risks and Completion

- Two builds on a pinned operator toolchain demonstrate reproducibility under recorded conditions, not a signed supply-chain or cross-platform guarantee. The private evidence store and the operator are trust boundaries.
- Check off tooling after synthetic verification; collect real proof after both tooling checkpoints. The procedure must work without the authoring files or holdout. Update checkboxes in the index only.
