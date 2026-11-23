# Phase 2: Independent Blind Evaluation

Plan: [index.md](index.md)  
Tasks: 3–4  
Depends on: Task 2's verifier contract for implementation; verified build and approved run ID for operator execution

## Objective

Measure exact delivered dependency coverage and retrieval quality on an independently authored and sealed corpus, without letting implementation or tuning see the cases, labels or results before freeze.

## Current-Code Evidence

| Path | Existing behavior |
|---|---|
| `src/lean_weave/benchmark/retrieval.py` | `load_cases`, `materialize_repository`, `evaluate_retrieval`, `precision_at_k`, `recall_at_k`: the installed corpus and answers are packaged; not a fresh blind source. |
| `tests/retrieval_dependencies/dev_eval.py` | `run_v1_arm` calls `DeterministicContextProvider.build("implement", ...)`; `run_v2_arm` uses a checked `create_context_packer(...).open_task(...)`; `interval_fully_covered` and `_score_units` illustrate exact delivery. The harness knows labels and is development-only. |
| `benchmarks/validation/v2-08-retrieval-thresholds.json` | Prior per-split/per-case non-regression precedent for file/symbol Recall@5 and missing dependencies. Do not run the old holdout or import its data. |

## Files to Change

- `benchmarks/release_blind_capture.py` (new): evaluator-side label-free subprocess capture from the installed wheel.
- `benchmarks/release_blind_score.py` (new): evaluator-only validation, scoring and receipt commands; use Phase 1 envelope helpers. No new product loader or CLI.
- `tests/test_release_blind.py` (new): synthetic-only capture and scoring tests.
- `docs/v2-release-evidence.md`: independent custodian playbook, preregistration and sealed evidence access. Case data, queries and labels live outside Git and are not new repository fixtures.

## Task 3: Seal the holdout and freeze rules

### Intent and implementation

1. Appoint a custodian who did not implement or tune the candidate. Keep inputs and oracle in separate owner-only external stores with an access log. Use separate OS identities or equivalent enforced read isolation: the capture worker receives only the read-only fixture and task inputs, an empty private scratch, and no oracle, credentials or network; no fixture code is executed. Custodian authors original cases without copying/adapting existing corpora or results. Record source origin/license, independence attestation, exact tree/query comparisons plus structural review, and a second evaluator's label review. Implementation receives only the protocol, synthetic dry runs and redacted results.
2. Freeze a versioned suite with at least 12 tasks across at least three independently sourced repository snapshots; at least eight dependency-heavy tasks, each with at least two labeled required references. Cover cross-module definitions/imports, aliases or relative imports, annotations/inheritance, and dependency paths across modules, with realistic tasks and a mix of simpler controls. Label expected file/symbol IDs for R@5/P@5 and exact required path/line intervals plus source digest for dependencies. Labels must have a nonempty denominator, unique IDs, matching source bytes, and be within the common admitted source scope and feasible under the preregistered caps; judge eligibility from authored inputs/labels before any candidate output. Never exclude a case after seeing its outcome. Pin exact fixture Git commit/tree and materialized file inventory per case; predeclare ordering, repetitions, budgets and fixed settings.
3. Before any blind candidate run, use Phase 1 `freeze-run` to pin input/oracle commitments (with a private nonce for the oracle), scorer/capture digests, protocol and gate revision, and the installed artifact/config. Require detached Ed25519 signatures from an independently approved custodian key over canonical freeze and final scoring receipts; pin the public key fingerprint outside the run and verify with an approved pinned offline verifier. Signatures bind the `run_id`, stage/status and referenced digests; the verifier rejects unsigned, wrong-key, or wrong-run receipts. The custodian's signed freeze follows creation of the run ID, avoiding a digest cycle. Reserve capture and score slots once, atomically seal each result, and never overwrite or re-score a revealed run.
4. Freeze V1 implement vs V2 L1 with dependency coverage enabled (`max_depth=2`, `max_definitions=8`, `max_tokens=1024`) from the same installed wheel. Feed identical neutral task text to V1 task description and V2 query/spec, with the same ordinary edit target. Use a bounded, reviewed tracked file inventory materialized identically for both; declare and validate the allowed readable universe separately from the observed selection. V1's native targeted scope and V2's whole-snapshot scan are intentionally different; `PackRequest.output_paths` is not a read restriction. Use native limits declared by arm and one preregistered numerical common cap on canonical rendered system/user message bytes and conservative input token count, plus the same output reserve (count, not generated usage). Pre-register the exact common counter, canonical encoding, budgets and arm profiles; prove the harness with public fixtures before freeze, with no truncation in the evaluator. Require an ordinary edit-anchor interval for both arms; no oracle paths, L2, fallback, ablations or cache warming. Limit conclusions to this context-only comparison.

### Contract, failure and logging

Freeze receipt contains artifact ID, fixture-set commitment, schedule/config/gate digests, tool hashes, timestamp, custodian identity and independence/exposure disposition. If there is no independent custodian, incomplete oracle, unauthorized disclosure, config changed, or inability to establish comparable scopes, emit `holdout_missing` or `holdout_invalid` and block. INFO: aggregate counts and IDs; DEBUG: schema/validation stages, no case names/text/labels; ERROR: reason codes, no hidden details outside restricted archive.

### Requirement evidence, tests and acceptance

Original Request B.1–6, B.8–11; v2-09 Original Request “Critical evaluation discipline.” `tests/test_release_blind.py` covers schema completeness, empty scored denominators, wrong revision/digest, duplicates, insufficient strata, premature exposure and missing freeze. Verify with `uv run --locked --offline pytest tests/test_release_blind.py`. Operator checks the signed/controlled freeze and immutable commitments inside the restricted store, without disclosing the oracle. Without a real sealed holdout this phase cannot certify the release.

## Task 4: Capture and score the frozen pair

### Intent and implementation

1. From fresh isolated fixture copies per arm, give capture only the task request, edit target, pinned repository, arm config and opaque case ID. Run V1 provider implement and V2 L1 packer from the verified wheel, using production calls identified above, not the old benchmark's hardcoded variants or the oracle-aware dev harness. Measure retrieval+materialization wall time by monotonic clock: three paired repeats with preregistered alternating arm order; report required V2 index-preparation latency separately and also report cold end-to-end latency including setup for both arms. No shared index, journal, model, or mutable fixture state. Verify fixture revisions and input equality before and after. Capture emitted whole source spans, ordered unique file/symbol IDs from delivered source (use AST definitions and pinned resolved spans, no map-only matches), status, context bytes, local estimated input tokens using the same counter and delivered payload representation, and timing. Refused/missing delivery is not silently counted as a successful zero-size pack.
2. Custodian seals capture hashes before scoring. Verify the pinned run and installed-worker identity and check captured span bytes against pinned fixture bytes using 1-based inclusive lines. Union overlapping or adjacent spans only for the same path and source digest. Count a dependency or edit anchor only when its entire labeled interval is covered. Use canonical file IDs and symbol IDs `(path, qualified_name, definition_start_line)` derived from the pinned AST; a symbol enters the rank only when its entire declaration header is covered. Order by the earliest emitted unit contributing to each fully covered item, then canonical ID for ties. Map rows never count. For the deduplicated ranks, divide relevant top-five hits by 5 for precision and by the relevant label count for recall. Report per-case and aggregate totals, median and nearest-rank p95 latency with sample counts, context bytes and estimated tokens. Repeats must agree on non-timing observations and are not extra cases. Preserve refusals, failures and missing slots; sign the final receipt over capture, scorer and result digests and publish only the redacted view.
3. Preregister required checks before freeze: all cases paired with source delivery and edit-anchor coverage on both arms, V2 zero required misses including dependency-heavy cases, no per-case loss versus V1 in file/symbol Recall@5, no cap violation or scope escape. Report Precision@5, size and latency; only explicit approved preregistered limits can gate these. A failed, invalid or missing result yields **Not ready**. After candidate-visible disclosure, retire the corpus for unbiased claims. After a blind failure preserve it and require a new development cycle and fresh holdout, with no relabeling, selective reruns or tuning against disclosed cases.

### Contract, failure and logging

`BlindReceiptV1` is a Phase 1 envelope for `blind` containing frozen schedule, artifact, config, dataset commitment, capture/scoring identities, completeness and individual required gate statuses. Raw custody and reuse/retirement metadata are append-only. INFO: paired counts and gate dispositions; DEBUG: sanitized timing and stage IDs; ERROR: `pair_incomplete`, `dependency_gate_failed`, `recall_gate_failed`, `scope_or_budget_violation`, `capture_nondeterministic`. Never output raw prompt, source or oracle on the public path.

### Requirement evidence, tests and acceptance

Original Request B.7–12 and acceptance 3, 7–8. Synthetic tests in `tests/test_release_blind.py`: labels not available to capture, equal source/config contracts, interval gaps and map-only negative, short rankings/duplicates, deterministic comparisons, omitted arm, failed V2 coverage, time kept diagnostic, preserving failures and retire-on-disclosure. Run `uv run --locked --offline pytest tests/test_release_blind.py`; operator follows the restricted capture/score commands in `docs/v2-release-evidence.md` on the real sealed corpus and verifies sanitized receipt with `verify-binding`. Only a genuine custodian attestation plus required gate passes is acceptable release evidence.

## Phase Risks and Completion

- Separation is meaningless if the implementer authors, sees or scores the holdout. Tests of the protocol use only synthetic inputs; the run is an operator/custodian action, not delegated to the implementation agent.
- Check off tooling after synthetic verification. After both tooling checkpoints the operator records sealed evidence or a blocked/failed disposition. A completed checklist is not a passed gate.
