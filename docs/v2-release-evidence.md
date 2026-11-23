[← Benchmarks](benchmarks.md) · [Back to README](../README.md)

# V2 Release Evidence Protocol

Plan: `v2-10-release-evidence`  
Audience: release operators and independent custodians  
Driver: `benchmarks/release_evidence.py` (stdlib-only, developer/operator tooling)

This protocol produces fail-closed release evidence bound to one clean Git commit
and one verified wheel. It does not change product defaults, publish tags, or
authorize live DeepSeek calls by itself.

## Principles

1. Evidence is collected from a **named full commit OID** with a clean detached
   worktree, never from a dirty authoring checkout.
2. Build twice; accept only identical wheel bytes.
3. Bind evidence to the installed wheel via `artifact-binding.json`; an
   environment variable wheel path is observational only.
4. Freeze a `run_id` before any blind, offline, or live stage.
5. Missing or invalid required evidence yields **Not ready**.
6. Known historical holdouts stay archival; blind evaluation uses a custodian
   sealed corpus outside the repository.

## Clean double build

1. Resolve the candidate to a full 40-character commit OID with an approved Git
   binary (no abbreviations, tags, or replacements).
2. Extract `benchmarks/release_evidence.py` as a blob from that commit into a
   private external directory. Record blob ID and byte digest.
3. Launch:

```bash
<pinned-python> -I -B <external>/release_evidence.py build \
  --repo <object-source-repo> \
  --commit <FULL_OID> \
  --output <external-evidence-root> \
  --build-inputs <pinned-build-inputs.json>
```

4. `build-inputs.json` pins Python, Git, uv, wheelhouse hashes, build
   constraints, reproducibility environment, and either `mode: real` or
   `mode: synthetic` (synthetic is for tooling tests only). Optional
   `source_date_epoch` overrides the commit-derived default.
5. The driver fail-closes when its own bytes differ from the committed blob,
   creates two detached worktrees, verifies clean status and tracked bytes,
   sets `SOURCE_DATE_EPOCH` from the commit timestamp, builds wheels
   externally, and writes `build-proof.json`. A mismatch preserves both
   artifacts and marks the proof failed.

## Artifact binding

After a matching double build, write `artifact-binding.json` under the evidence
root (private relative wheel path, commit/tree, lock and `pyproject.toml`
digests, embedded `BuildProofV1`). Revalidate at every stage:

```bash
<pinned-python> -I -B <external>/release_evidence.py verify-binding \
  --manifest <evidence>/artifact-binding.json \
  --repo <object-source-repo>
```

Install from the verified wheel only (`uv pip sync` hashed export, then
`uv pip install --no-deps <wheel>`; see `build_install_plan`). Revalidate
installed `lean_weave` bytes against the wheel `RECORD`. Reject editable
installs, import shadowing, and `PYTHONPATH` injection. Binding rechecks both
preserved build wheels and four clean observations. Source pytest is labeled
`source-regression`; installed runs are `installed-wheel`.

## Freeze a run

```bash
<pinned-python> -I -B <external>/release_evidence.py freeze-run \
  --manifest <evidence>/artifact-binding.json \
  --preregistration <prereg.json> \
  --output <evidence>/runs
```

Preserve `run-id-pin.json` outside the mutable receipt directory. Later stages
and `decide` require this independent pin.

## Blind evaluation (custodian)

Helpers: `benchmarks/release_blind_capture.py`, `benchmarks/release_blind_score.py`.

### Custodian playbook

1. Author an original sealed corpus (≥12 tasks, ≥3 repositories, ≥8
   dependency-heavy cases) outside Git. Keep oracle labels in a separate
   owner-only store with an access log.
2. Validate the suite schema before freeze (`release_blind_score.py validate-suite`).
3. Freeze the run with Phase 1 `freeze-run`, then sign freeze/score receipts with
   an independently approved Ed25519 key:

```bash
openssl pkeyutl -sign -inkey custodian.pem -rawin -in freeze.json -out freeze.sig
release_blind_score.py verify-signature \
  --openssl <pinned-openssl> \
  --public-key custodian.pub.pem \
  --message freeze.json \
  --signature freeze.sig
```

4. Capture with label-free specs only. Use `--mode installed --python <env>` so
   the worker runs V1 implement / V2 L1 packer from the verified wheel (synthetic
   mode is for tooling tests). Seal capture digests before scoring.
5. Score once; never overwrite. File and symbol Recall@5 are gated. On failure,
   preserve the receipt, mark **Not ready**, retire the corpus
   (`retire-corpus`), and require a new development cycle plus a fresh holdout.

Implementation agents must not author, inspect, or tune against the sealed
holdout. Synthetic dry-runs use public fixtures only. Automatic pytest
regressions load `tests/benchmarks/fixtures/retrieval/cases.json` (development
only); the packaged historical catalog remains on disk for audit.

## Offline and live dispositions

```bash
# Offline receipt (operator supplies source + installed reports)
... release_evidence.py offline --repo ... --manifest ... \
  --evidence-root ... --expected-run-id-file .../run-id-pin.json \
  --source-report ... --installed-report ... \
  --required-nodes-file .../required-nodes.json \
  --ledger-path .../pytest-ledger.json \
  --require-network-namespace --output ...

# Live disposition (default under this plan: not authorized)
... release_evidence.py live --policy policy.json \
  --expected-run-id-file .../run-id-pin.json --output ...
# Authorized measured runs may pass --usage-json with provider counters only.
```

Without separate authorization and credentials, live remains
**Live: Not evaluated**. Usage aggregation never substitutes estimates; missing
coverage yields an unavailable per-success ratio, not zero.

## Decision

```bash
... release_evidence.py decide \
  --repo ... --commit <FULL_OID> \
  --evidence-root ... --policy policy.json \
  --output ... [--manifest ...] [--expected-run-id-file ...]
```

Exit `0` only for **Ready (for opt-in)**; `1` for a reproducible **Not ready**;
`2` for malformed invocation. Historical `docs/v2-readiness.md` remains an
audit record and is not rewritten as new evidence.

## Offline workflow verification

From a verified detached worktree of the candidate commit:

1. Export hashed requirements with `--group dev --group spikes`.
2. Launch under a network namespace (`unshare --user --map-root-user --net`) when
   available; block if the namespace prerequisite fails.
3. Run source pytest with the ledger plugin:

```bash
<dev-python> -m pytest -p tests.release_pytest_ledger \
  -m 'not network' -p no:cacheprovider \
  --release-ledger-output <external>/pytest-ledger.json \
  --junitxml=<external>/offline.xml
```

4. From the verified wheel environment, smoke `wl` / `lean-weave` and run the
   installed paired workflow matrix + compare against
   `benchmarks/validation/v2-08-workflow-thresholds.json`.
5. Record `source-regression` and `installed-wheel` separately via
   `release_evidence.py offline`.

Automatic retrieval regressions use the development catalog under
`tests/benchmarks/fixtures/retrieval/`; they must not open the historical
holdout cases.

## Logging and redaction

Control verbosity with `LEAN_WEAVE_LOG_LEVEL` / `LOG_LEVEL`. Evidence logs must
not emit credentials, absolute developer paths, fixture labels, or raw source.
