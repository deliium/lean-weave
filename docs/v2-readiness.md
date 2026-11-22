[← Benchmarks](benchmarks.md) · [Back to README](../README.md)

# V2 Readiness Report

Plan: `v2-08-e2e-optimization`  
Evidence source revision: `dcde3b42cd8294541e19e9ec6d5c06ba358bf0d2`  
Evidence-time code-tree digest: `sha256:32d03c710d9fdf8290131b91904cf57f5536033c3f3a4a5af907c75766bee370`  
Live: **not run** (not authorized by the planning request)

## Verdict

**Not ready.**

The complete V2 offline path is functional, V1 remains the default and operational,
the paired workflow correctness gate passes, and cache invalidation is
incremental-equivalent to a clean rebuild. The release gate fails because the
**retrieval holdout split still regresses required-dependency coverage**:
a general enclosing-declaration fix reduced misses from four to two, but V2
still packs 436 selected source tokens versus 358 for V1 and misses two labeled
required dependency spans versus one for V1. The remaining misses are a
referenced `Invoice` class that is not selected (`holdout_diagnose`) and a
client import header (`holdout_invoice_tight`). Fixing those would require
new dependency/header selection rules; doing so against this holdout is
explicitly out of scope for this plan.

V1 stays the production default in every case. Do not switch defaults until the
holdout regression is fixed and the retrieval gate is rerun.

## 1. Implementation summary

All V2 dependency bundles are present at the inspected revision and were
exercised together:

- code index, repository map, task-aware retrieval, token-aware packing,
  disposable context cache, workflow context integration and attributed
  attempt telemetry;
- paired V1/V2 offline workflow execution through the real `FeatureSession`,
  managed worktrees, `StateStore`, deterministic oracles and the DeepSeek
  gateway seams;
- strict V2, V2-with-V1-fallback and V1 context modes;
- approximate source-tree identity and effective-condition capture (fixture
  HEAD, manifest digest, verifier argv/oracle hashes, write allowlist, model
  settings, workflow budgets, context knobs, Python/Git versions, lock digest);
- declared schedules with alternating variant order, per-metric coverage and
  paired regression gates (policy version 2);
- bounded attempt/context evidence and dispatch classification
  (`no_send` / `observed` / `unknown`);
- guarded paired live admission and schedule code. It was implemented and
  unit-tested without network access, but no live run was authorized.

The two new pinned fixtures (`refactor_preserve_behavior`, `test_generation`)
extend the installed corpus to nine cases.

## 2. V1 versus V2 token comparison

Provenance: `offline_scripted` (mock provider). Provider-reported counters are
scripted, not DeepSeek measurements. `estimated_input_tokens` is a local
preflight estimate and is not API spend.

| Case | V1 total | V2 total | V1 estimate | V2 estimate | Δ estimate |
|---|---:|---:|---:|---:|---:|
| budget_exhaustion | n/a (no send) | n/a (no send) | n/a | n/a | n/a |
| bug_fix | 42 | 42 | 2,910 | 2,458 | −452 |
| insufficient_context | 14 | 14 | 630 | 558 | −72 |
| multi_file_feature | 70 | 70 | 5,075 | 4,449 | −626 |
| refactor_preserve_behavior | 42 | 42 | 3,191 | 2,741 | −450 |
| single_file_feature | 42 | 42 | 2,944 | 2,489 | −455 |
| test_generation | 42 | 42 | 3,065 | 2,653 | −412 |
| test_repair | 42 | 42 | 3,046 | 2,569 | −477 |
| verify_then_repair | 70 | 70 | 4,824 | 4,320 | −504 |

Paired aggregate (`repeats=3`, 27 scheduled pairs):

- total-token delta median: `0`; incurred scripted totals: V1 `1050`, V2 `1050`;
- estimated-input delta median: `−453.5`;
- logical-call delta median: `0`; workflow-retry delta median: `0`;
- duration delta median: `+19.5 ms` (V2 local overhead on these tiny fixtures);
- expansion attempts: none observed in either arm.

The scripted transport returns fixed usage for each request, so equal totals are
expected and are **not** evidence of DeepSeek savings. The local estimates show
that V2 prepares smaller prompts on every positive fixture, which is the
mechanism worth validating live.

### 2.1 Estimated input components

All values use the packing `conservative_estimate` counter over sent request
components only. They are explanatory slices and are not assumed to sum to the
complete request estimate. `156` requests were sent; no unsent reservations were
observed.

| Component | V1 | V2 |
|---|---:|---:|
| system instructions | 80,328 | 55,248 |
| task instructions | 0 (untagged) | 17,109 |
| repository map | 0 | 2,262 |
| approved source | 0 (untagged) | 16,167 |
| editable revisions | 0 | 0 |
| other user content | 48,555 | 0 |
| accumulated tool messages | 5,700 | 5,700 |
| tool schemas | 51,840 | 51,840 |
| response format | 528 | 528 |

V1 folds task/source text into an untagged user message (`other_user`); V2 emits
explicit task, map and source slices. Tool schemas and tool messages are
identical across variants, so the observed estimate difference comes from the
stage instructions and the packed context shape, not from repeated tool output.

For one `multi_file_feature` V2 arm, the stage request components were: system
`3,581`, task `918`, map `131`, approved source `1,046`, tool messages `422`,
tool schemas `3,840`, response format `22`. End-to-end wall was `252 ms`:
workflow `209 ms`, oracle `17 ms`, collection `26 ms`.

## 3. Retrieval quality results

Provenance: `local_estimate`. Scores are descriptive medians over nine labeled
cases; development and holdout are reported separately. Duplicate emitted lines
were `0` in both strategies.

| Split | Variant | P@5 | R@5 | Symbol R@5 | Missing deps | Missing symbols | Selected tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| development | V1 | 0.300 | 1.000 | 1.000 | 1 | 1 | 246 |
| development | V2 | 0.400 | 1.000 | 1.000 | 0 | 0 | 246 |
| holdout | V1 | 0.200 | 1.000 | 1.000 | 1 | 0 | 358 |
| holdout | V2 | 0.200 | 1.000 | 1.000 | 2 | 0 | 436 |

Development ranking improved at P@5 and required-symbol/dependency coverage
improved to zero misses. Holdout ranking and recall are unchanged. A general
"include the enclosing declaration for a nested anchor" fix reduced holdout
required-dependency misses from four to two; pack size remains larger because
the added declarations are real context. The remaining two misses require
separate referenced-definition/import-header selection work.

The retrieval policy
`benchmarks/validation/v2-08-retrieval-thresholds.json` therefore fails:

```text
missing_dependency_count_holdout_worst_delta: failed (observed 1.0, tolerance 0)
missing_dependency_count_holdout_total_delta: failed (observed 2.0, tolerance 0)
```

The gate uses the worst per-case delta and the aggregate total delta, not a
median, so a per-case regression cannot hide behind a swap.

### 3.1 Controlled ablations

All pairs are compatible (`18` paired rows each, `local_estimate`, same fixture
identity and source tree). These are exploratory, not release gates.

| Experiment | P@5 dev Δ | R@5 dev Δ | R@5 holdout Δ | Missing deps dev / holdout Δ | Selected tokens dev / holdout Δ |
|---|---:|---:|---:|---:|---:|
| map off (`v2_map_off`) | 0 | 0 | 0 | 0 / 0 | 0 / 0 |
| graph off (`v2_no_graph`) | 0 | 0 | 0 | 0 / 0 | 0 / 0 |
| tight source budget (`v2_tight`) | 0 | 0 | 0 | 0 / 0 | −81.5 / −258.0 |
| cache cold vs disabled | 0 | 0 | 0 | 0 / 0 | 0 / 0 |
| cache warm vs cold | 0 | 0 | 0 | 0 / 0 | 0 / 0 |

On this corpus, map rows, graph resolution and cache state do not change the
selected ranked output. The tight budget materially reduces selected source
tokens with no measured precision, recall or coverage loss, but it is not a
substitute for the holdout dependency fix and no default was changed.

## 4. Workflow reliability results

Paired workflow run: nine cases × two variants × three repetitions = 54 arms.

- 54/54 arms correct, including both expected-negative fixtures;
- expected outcomes: all successes verified by independent oracles; both
  `insufficient_context` arms failed with `invalid_plan` and no workspace
  changes; both `budget_exhaustion` arms stopped with `feature_token_limit`
  before any transport send (`dispatch=no_send`, proven by an independent
  transport counter of zero);
- `verify_then_repair` performed exactly one bounded repair in each arm;
- no verification LLM sends, no unbounded retry, no unexpected workspace path;
- managed-worktree changes matched the approved write allowlists;
- review/approve, stale approval, interruption/resume, uncertain-effect,
  budget exhaustion across resume and isolation scenarios remain covered by
  their existing CLI regression suites;
- context expansion had no production occurrences in the scripted matrix; the
  dedicated expansion suite exercises the bounded and denied paths.

## 5. Cache effectiveness

Workload: two tiny packaged retrieval cases, five measured iterations per mode,
priming outside the measured window. `llm_calls=0`, `api_tokens_measured=false`.

| Workload | Mode | Median wall | Median CPU | Rank calls | Pack builds | Hits |
|---|---|---:|---:|---:|---:|---:|
| bug_fix_add/no_index | disabled | 6.00 ms | 3.17 ms | 10 | 5 | 0 |
| bug_fix_add/no_index | cold | 14.46 ms | 11.15 ms | 10 | 5 | 2 |
| bug_fix_add/no_index | warm | 2.88 ms | 2.88 ms | 0 | 5 | 20 |
| bug_fix_add/indexed | disabled | 7.66 ms | 4.00 ms | 10 | 5 | 0 |
| bug_fix_add/indexed | cold | 16.29 ms | 12.85 ms | 10 | 5 | 1 |
| bug_fix_add/indexed | warm | 5.83 ms | 5.73 ms | 0 | 5 | 31 |
| multi_file_banner/indexed | disabled | 8.02 ms | 4.54 ms | 10 | 5 | 0 |
| multi_file_banner/indexed | cold | 14.25 ms | 12.23 ms | 5 | 5 | 1 |
| multi_file_banner/indexed | warm | 7.92 ms | 7.81 ms | 0 | 5 | 49 |

Cache conclusions:

- warm reuse eliminates counted ranking work and is faster on `bug_fix_add`,
  but cold construction is expensive and warm is **not** faster than disabled
  on `multi_file_banner/indexed` (8.05 ms vs 6.50 ms median).
  Tiny workloads exaggerate bookkeeping; no cache-speed default was changed;
- selected output signatures were identical across disabled/cold/warm;
- incremental index updates compared equal to a clean rebuild after unchanged,
  edit, restore, rename, delete and recreate operations;
- per-kind hits, samples and separate index-build timings are now persisted;
- worktree namespace isolation, stale/corrupt recovery and cross-checkout tests
  remain green;
- index timings on the packaged fixture: build `1.80 ms`, unchanged update
  `0.90 ms`, edit `1.32 ms`, rename `1.26 ms`, delete `1.07 ms`, recreate
  `1.18 ms`, clean rebuild `1.15 ms`; incremental status matched clean.

### 5.1 Scaled synthetic workloads

Synthetic trees hold the query target constant and grow only off-scope fanout or
importer density. All are labeled `synthetic` and use zero LLM calls.

| Workload | Files | Median wall | Median CPU | Fact extractions | Output equal |
|---|---:|---:|---:|---:|---|
| 100 off-scope files | 100 | 20.23 ms | 16.83 ms | 404 | yes |
| 1,000 off-scope files | 1,000 | 148.86 ms | 144.84 ms | 4,004 | yes |
| 50 dense importers | 50 | 14.65 ms | 12.08 ms | 204 | yes |

Fact extraction grows with file fanout (roughly four extractions per off-scope
file) while ranking and pack work stay bounded. This is a scaling signal for
larger repositories, not a production latency threshold.

**Tuning decision: no production default changed.** No candidate met the
pre-registered bar (a reproducible win with no required regression). Cache-speed
defaults stay off because warm was not uniformly faster; retrieval weights and
pack budgets were not changed against holdout; workflow verification and
authority were not touched.

### 5.2 Release eligibility

The run explicitly records a release-binding object rather than treating an
unknown wheel identity as matching evidence:

| Field | Value |
|---|---|
| Installation kind | source |
| Source revision | `dcde3b42cd8294541e19e9ec6d5c06ba358bf0d2` |
| Code-tree digest | `sha256:32d03c710d9fdf8290131b91904cf57f5536033c3f3a4a5af907c75766bee370` |
| Dependency-lock digest | `sha256:27a7ead77a9e0804f2df91a653fe790b84ffff18adac282fa6c20be0bd0b97ab` |
| Wheel SHA-256 | `sha256:f674dffc5fa4f6d320ea3c8d096394ca8ab8587e086c286c7a596f7a52910446` |
| Release eligible | **false** |
| Reasons | `source_tree_dirty` |

A wheel hash is now recorded and bound through the benchmark environment, but
the source tree was dirty during collection, so formal release eligibility
remains **false**. A clean committed build with the same binding is required
before this evidence can be attached to a formal release.

## 6. Correctness regressions

- **Retrieval holdout required dependency (blocking):** V2 misses four labeled
  required dependency spans versus one for V1, and emits 73 more selected source
  tokens on holdout. The inspected `holdout_invoice` pack selects the class
  method and function but omits the `class Invoice:` header.
- **Local duration overhead (non-blocking):** V2 median arm duration is about
  19 ms higher on the tiny offline corpus. This is local bookkeeping, not LLM
  latency, and is not a token-savings claim.
- No workflow correctness, verification, scope, approval, resume or isolation
  regression was observed.

## 7. Known limitations

- Scripted providers do not measure DeepSeek token counts; no live savings claim
  is supported.
- The retrieval corpus is small. Holdout scores have small denominators and are
  used only for final validation, not tuning.
- `selected_source_tokens` is a conservative local estimate, not provider input.
- Component estimates are explanatory slices; they are not additive and do not
  replace a complete request estimate.
- Cache measurements cover two tiny packaged cases plus bounded synthetic
  fanout; latency is not a production threshold.
- The live matrix was implemented but not exercised against the real endpoint.
- The evidence tree was dirty during collection and no wheel hash is available;
  release eligibility is explicitly false until a clean build is bound.
- Source-tree identity is a digest, not a signed build provenance chain.

## 8. Remaining technical risks

- A packing fix for enclosing class/declaration headers could change prompt
  sizes and retrieval scores; rerun both dev and holdout after any change.
- Warm cache can be slower than disabled on some workloads; enabling it by
  default could regress latency without measured benefit.
- Live DeepSeek totals may diverge from local estimates because tokenizers,
  hidden reasoning, retries and provider caching are remote behaviors.
- Larger repositories may expose ranking fan-in or proof-size behavior not
  visible in the tiny corpus. The synthetic 1,000-file run already shows fact
  extraction growing roughly linearly with fanout (4,004 extractions).
- Benchmark metadata is additive but old readers do not understand new fields;
  forward reader compatibility remains one-way as documented.

## 9. Recommendations for future milestones

1. The enclosing-declaration fix is in place and covered by a focused
   regression. Extend selection to referenced definitions (annotation/base
   types) and leading import headers only through a separate planned change;
   do not tune those rules against this holdout.
2. Rerun the frozen retrieval policy on both splits after that work; keep the
   change only if required dependency coverage reaches zero misses without a
   required precision/recall loss.
3. After the retrieval gate is green, obtain separate live authorization and run
   the paired live schedule with fixed rates, limits and deadline.
4. Rebuild evidence from a clean committed revision and bind the built wheel
   hash to that revision and lock digest.
5. Consider an opt-in cache only after a larger workload shows a reproducible
   latency win over disabled mode.

## Evidence inventory

Private operator directory: `/tmp/lw-evidence` (outside the checkout).

| Artifact | SHA-256 | Command |
|---|---|---|
| `workflow.json` | `7a463e6d136e89c5df8dfca7c552b62a1239c28d3ea4527697fd5e3bbb80654f` | `wl benchmark run --suite workflow --variant v1 --variant v2 --repeats 3` |
| `retrieval.json` | `7231424f87211bf95b1229a4bf84c281bde67febf6a97d2ac4bb6a3187521ad1` | `wl benchmark run --suite retrieval --variant v1_default --variant v2_default --repeats 2` |
| `retrieval-ablations.json` | `86f965c6bc37c7c148f1a9f74d3dafbc8f439eaeff791dc8677f8f1dd5d7672b` | `wl benchmark run --suite retrieval` with the seven V2 ablation variants |
| `cache.json` | `d1c7bc86833467cb8be744781ef19f5e8e34cf82cf71bf584a7e4b87b4d82498` | `python -m benchmarks.context_cache --json` |
| `cache-scaled.json` | `b31ec0bbac36c561f34f138ee7a82b228bf822dc4f9b1eb9326f16e44e36862c` | `python -m benchmarks.context_cache --json --scaled` |
| `workflow-gate.json` | `9131309ba084aaac0b87fdcf53951a7dacffa7dfffc8437d38731a34f27648ae` | `wl benchmark compare ... --thresholds benchmarks/validation/v2-08-workflow-thresholds.json --json` |
| `retrieval-gate.json` | `bc0239e3bf448296f2c59ea9fc297546135a5a07466aa7cc5ddba70bde003416` | `wl benchmark compare ... --thresholds benchmarks/validation/v2-08-retrieval-thresholds.json --json` |
| `ablation-map.json` | `d63965af28b33264ced968fd529d573d5253fd614166a9b765ab11dcdebd0d3e` | `wl benchmark compare ... --axis map --baseline-variant v2_default --candidate-variant v2_map_off` |
| `ablation-graph.json` | `c3655b609a8a490b1594cec8c2f2d00de7638c3d6c01a6e9fe5acf129121ec3e` | `... --axis map --baseline-variant v2_map_off --candidate-variant v2_no_graph` |
| `ablation-budget.json` | `23e32eb934f3caa31b2f5c54df8783094136021d7e75d3926fe1e57aa7841df7` | `... --axis budget --baseline-variant v2_default --candidate-variant v2_tight` |
| `ablation-cache-cold.json` | `8af5c02f9040f345a97a00b6eb2a00768d8bd9cfeee57f16073efff97d330abe` | `... --axis cache --baseline-variant v2_cache_disabled --candidate-variant v2_cache_cold` |
| `ablation-cache-warm.json` | `c01153d9bf2ac7ccf130f2a773c0a975d6d72dba0649c54dd37ea7444f883df4` | `... --axis cache --baseline-variant v2_cache_cold --candidate-variant v2_cache_warm` |
| `/tmp/lw-wheel/lean_weave-0.1.0-py3-none-any.whl` | `f674dffc5fa4f6d320ea3c8d096394ca8ab8587e086c286c7a596f7a52910446` | `uv build --wheel --out-dir /tmp/lw-wheel` |
| `workflow.md` | `132f315a1043c5b7a3da66533e71ff2050d21ce0bd94b0f20ed7820db4cf7fb2` | `wl benchmark report /tmp/lw-evidence/workflow.json` |

Raw prompts, source text, patches, operator config bodies and credentials are
not stored in any of these artifacts.

## Documentation update log

| Page | Change |
|---|---|
| `docs/benchmarks.md` | Paired schedules, condition capture, per-metric coverage, v2 gates, paired live opt-in |
| `docs/architecture.md` | Release-validation status and links to V2 evidence |
| `docs/code-index.md` | Index update-timing and incremental-equivalence measurement |
| `docs/repository-map.md` | Map-off / graph-only experimental rows and limits |
| `docs/context-retrieval.md` | Split-aware P@K/R@K, missing symbols/dependencies, holdout regression |
| `docs/context-packing.md` | Pack accounting fields, missing-dependency observation, readiness link |
| `docs/context-cache.md` | Warm/cold latency observations, index update timing, no default speed claim |
| `docs/getting-started.md` | Pointer to the readiness report |
| `docs/worktree-workflow.md` | Resume/approval evidence remains unchanged; no behavioral change |
| `docs/cli.md` | Paired live command form and limits |
| `README.md` | Link to this report |
