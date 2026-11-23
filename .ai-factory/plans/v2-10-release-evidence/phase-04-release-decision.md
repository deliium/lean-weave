# Phase 4: Release Decision and Audit

Plan: [index.md](index.md)  
Task: 7  
Depends on: Tasks 2, 4, 5 and 6's receipt contracts for implementation; Tasks 1–6 evidence or explicit omissions for operator assessment

## Objective

Generate a deterministic, fail-closed verdict for one exact artifact. Preserve evidence even when a gate fails or an operator cannot supply it.

## Current-Code Evidence

- `src/lean_weave/benchmark/compare.py::compare_runs` and CLI `benchmark compare` establish benchmark pairing, not artifact identity or overall readiness.
- `docs/v2-readiness.md` is the prior **Not ready** record. Its verdict and §3 cite two post-fix V2 missing spans vs one V1; §6 retains the earlier four-miss observation. §5.2 records a wheel hash and dirty status; §7's contrary missing-hash bullet is historical drift. The report and catalog are not to be rewritten or used as new evidence.
- v2-08 `phase-03-release.md` Task 9 separates optional live from required safety/correctness/reproducibility and does not authorize a default change.

## Files to Change

- `benchmarks/release_evidence.py`: `decide --repo REPO --commit FULL_OID --evidence-root DIR --policy FILE --output NEW_DIR [--manifest FILE] [--expected-run-id DIGEST]`; write versioned machine decision and matching Markdown view into a new attempt directory.
- `tests/test_release_evidence.py`: release gate truth table and integration across manifests and artifacts.
- `docs/v2-release-evidence.md`: end-to-end operator procedure and evidence index schema.
- `docs/v2-readiness.md`: add a clearly separated current decision pointer/section only after real execution; retain the historical report body, inventory and prior failures unchanged. The bound Markdown report itself is outside the frozen source tree.

## Task 7: Enforce release eligibility

### Intent and implementation

1. An absent optional argument or nonexistent expected evidence file means `missing`, not a CLI usage error. Emit **Not ready** without inventing artifact identity (use requested commit separately from verified commit, and null unavailable fields). If supplied, reverify manifest against commit/tree, lock and both wheel bytes. Recompute `run_id` and compare it with the separate operator pin, verify exact stage expectations and `run_id` in every receipt, and verify the Phase 2 signatures using the independently approved key fingerprint. Require one create-once receipt per expected stage; verify actual referenced bytes and reject mismatches, unsupported records and duplicate/conflicting stage receipts. The trusted auditor needs restricted read access to the original holdout evidence, not merely the public summary. Never interpret equal unknowns as a match.
2. Evaluate build/reproducibility/clean tree, binding, offline correctness/safety, blind independence/comparability/retrieval and policy/live disposition as named gates with `pass`, `fail`, `missing`, `invalid` or `not_required`. Follow Task 6's live state table: approved optional and verified no dispatch → **Live: Not evaluated**; optional with valid incomplete measured usage but independently established safety and no blocking issue → **Live: Inconclusive** and no savings claim; incomplete required live, invalid custody/binding, unknown safety or observed blocking regression → **Not ready**. Only all required passes permit **Ready (for opt-in)**. A new pass cannot erase historical failure; blind failure requires a new development cycle and fresh evaluation. V1 remains default.
3. Emit JSON `ReleaseDecisionV1` plus a deterministic Markdown view from that same object: commit, wheel hash, source/build/lock/tool details, evidence inventory and references, blind comparison and raw denominators, offline and anomaly results, live status, offline scripted/estimated vs actual measured columns, unresolved issues and verdict. Expose only redacted identifiers and aggregates; keep credentials, local paths and private holdout details out. Return 0 only for Ready, 1 for a reproducible Not ready verdict, 2 for malformed invocation/output failure (no eligible report). Reserve fresh paths; retain past reports and hashes even on failure. No automatic publication, commit, tag, push, default change or decision override.

### Contract, failure and logging

Missing prerequisites must produce truthful gate statuses even when no records exist; reporting success is not a release pass. INFO: artifact ID, verdict, gate states; DEBUG: normalized rule and receipt fields; ERROR: drift, duplicate/mixed records, unsafe output and invalid policy. No logs with case/label/source text or secrets.

### Requirement evidence, tests and acceptance

Original Request E, Release gate, acceptance 5–9; v2-08 Task 9 reconciled with explicit D policy and A exact artifact. Test missing manifest or run ID, rewritten freeze, same-wheel cross-run swap, untrusted signer, substituted wheel, historical reuse, failed blind, optional no-dispatch/inconclusive/wrong binding, required live unrun, and a verified live safety regression despite incomplete usage. Exercise read-only use of `benchmarks/baselines/v1-offline-baseline.json` without running the old holdout; it cannot satisfy the new schema. Run `uv run --locked --offline pytest tests/test_release_evidence.py`; after both tooling checkpoints the operator runs `decide` twice with unchanged inputs in distinct output directories and expects deterministic gates/content. Mutate only copies for negative tests.

## Phase Risks and Completion

- Implementation tests may prove the verifier, not readiness. For submission, the operator commits tooling/config before selecting the candidate, then runs the documented external procedure. Any later docs-only navigation update is not the certified commit and must explicitly point to, not recertify, the original identity.
- Check off tooling after synthetic verification. After both tooling checkpoints the operator archives a truthful verdict, including any required evidence that is still missing. No phase checkboxes outside `index.md`.
