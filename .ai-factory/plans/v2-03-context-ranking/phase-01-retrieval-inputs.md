# Phase 1: Retrieval contracts and source evidence

Plan: [index.md](index.md)  
Tasks: 1–2  
Depends on: completed V2-01 and V2-02

## Objective

Make one explicit, bounded offline query produce canonical source evidence without changing any V1 result, workflow scope, or search flag.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/builder.py` | `ContextBuilder.build`, `_build_indexed_map_preview` | V1 scope filtering comes after default AST discovery; optional map is a separate map-only path. Do not wrap the builder as an admission or ranking primitive. |
| `src/lean_weave/context/ranking.py` | `tokenize`, `rank` | V1 ranks path/name substrings and imports, not text hits. Its lexicographic order is the evaluation baseline. |
| `src/lean_weave/context/repository.py` | `Repository.scan_python_sources`, `_StrictScanner` | Existing descriptor-relative reads, bounded directory collection, ignore witnesses, and quarantine are stronger than V1 preview inventory admission. |
| `src/lean_weave/context/search.py` | `_line_table`, `_payload`, `_rg_environment`, `search` | V1 already searches admitted canonical text on stdin, with Python fallback; single-literal file/hit semantics must stay unchanged. |
| `src/lean_weave/config/models.py` | `ConfigModel`, `ContextConfig` | Configuration is strict; the new settings can be nested without changing how existing fields validate. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/config/models.py` | modify | Strict retrieval settings and signal weights |
| `src/lean_weave/context/retrieval_models.py` | create | Typed request, source references, reasons and results |
| `src/lean_weave/context/repository.py` | modify | Add strict text scan entry point without changing the Python-mode path |
| `src/lean_weave/context/search.py` | modify | Add snapshot-based batched lexical evidence; retain V1 `search()` |
| `src/lean_weave/context/retrieval_query.py` | create | Deterministic query parsing and term expansion |
| `tests/context/test_retrieval_inputs.py`, `tests/context/test_retrieval_search.py` | create | Contract, source admission, and backend parity tests |

## Task 1: Define retrieval and admission contracts

### Intent

Give the ranker a new typed preview API and an explicit source boundary. Existing `ContextQuery`, `ContextPack`, and stage contracts are not extended.

### Implementation Steps

1. Add `ContextConfig.retrieval: RetrievalConfig` with a default factory. Add `top_k: int = 10` (1–100), and nested `weights: RetrievalWeights`. Use strict integers 0–100 per key and, after defaults are applied, require at least one of `exact_symbol`, `name`, or `lexical` to be positive; otherwise no direct candidate can qualify. Defaults in reason order: `exact_symbol=36`, `name=24`, `lexical=24`, `import_dependency=8`, `reverse_dependency=8`, `git_diff=5`, `related_test=8`, `map_importance=3`. A partial YAML mapping overrides only named defaults. No flag or YAML enables retrieval inside a workflow.
2. Add frozen typed models (strict validation at request/config boundaries) in `retrieval_models.py`. Use `RetrievalRequest(query, task_kind="feature", kind="all", top_k=None)`, with task kinds `bugfix|feature|refactor|test|diagnose` and kinds `all|file|symbol`. Validate strip/nonempty, max 2,000 chars, single line without controls (`str.isprintable()` on characters after strip, reject controls before stripping), and reject credential-shaped query text with the existing `contains_secret` guard and a generic error. `top_k=None` resolves to config; an explicit value uses the same 1–100 bound. No paths or workspace selectors in this contract: a path in query text is evidence only. This is an explicit whole-checkout preview, not a stage helper.
3. Define `RetrievalHit` with `kind`, `path`, nullable `symbol_id`, nullable `qualified_name`, nullable `definition_start_line` (present for symbols, separate from a shifted lexical window), `start_line`, `end_line`, `source_digest`, `source_kind` (`indexed_definition|live_definition|lexical_window|file_head|empty_file`), `relevance_score` (integer 0–1000), `ranking_reasons` (ordered nonzero scored `SignalReason(signal,value,weight,weighted_points)` records), `related_dependencies` (tuple of `DependencyEvidence(direction,related_path,importer_path,start_line,end_line,source_digest)`), and `estimated_token_cost` (nonnegative integer). Ranges are inclusive, 1-based, and both null only for an empty file. `RetrievalResult` contains `hits`, `task_kind`, `kind`, resolved `top_k`, fixed-order `degraded_reasons`, and `truncated` (candidate/evidence caps or more eligible rows than `top_k` after dedup; deliberate policy exclusions do not count). No snippets, signatures, raw query echo, timestamps, absolute paths, or search-tool output.
4. Add `Repository.scan_retrieval_sources()` using the existing strict scanner session pattern with `is_supported_text` instead of `is_python` as the file type predicate. Reuse the scan result fields via a common `SourceScan` type (keep the `PythonSourceScan` name as a compatibility alias); retain the old Python mode defaults. For retrieval, freeze the effective admission policy at session creation: resolved root, state directory, protected paths, exclusion tuple and all three scanner limits (`candidate_bytes_max`, `scan_bytes_max`, `visited_entries_max`). Compile exclusions from that same tuple and bind the opened root device/inode to the session at open. Use these captured values both to scan and to compare policy later, not a mutable `Repository` or `ContextConfig` reference. Preserve no-follow traversal, single-link regular-file admission, ignore uncertainty and witnesses. In retrieval mode require canonical UTF-8 printable relative paths (reject rewritten paths and drive prefixes); if stat fails during enumeration, mark the scan incomplete. Revalidation must verify the canonical root path still identifies the opened root as well as the existing descriptor/files/ignore witnesses. Preserve Python mode behavior. `ContextRetriever` will consume only captured admitted snapshots, never `Path.read_text`, Git paths or database rows as source authority.

### Required Interfaces and Contracts

- Public retrieval models are additive. Do not change `ContextProvider.build`, `ContextBundle`, `ContextQuery`, stage rendering, repository defaults, or index schema/policy keys.
- Stable degraded codes: `source_scan_incomplete`, `source_changed`, `source_unavailable`, `index_missing`, `index_stale`, `index_incomplete`, `index_unavailable`, `lexical_python_fallback`, `git_unavailable`. Emit each at most once in this order; no raw exception strings or denied paths.

### Requirement Evidence

- Original Request 1, 5–8, 11–16 and expected fields; reconciliation **Inputs**, **Compatibility**. Rules: secrets stay out of results/logs and cannot expand scope.

### Error Handling and Logging

- Invalid config goes through existing safe config validation; invalid request fails with `InvalidContextRequest` and does no I/O. DEBUG logs only limits, booleans and admitted aggregate counts; WARNING for incomplete admission, without unsafe names/content.

### Tests

- `test_retrieval_inputs.py`: backward-compatible config defaults, overrides and strict rejected values (bool, negative, extra key, all zero, bonus-only); whitespace, oversize, controls, credential-shaped request, unknown mode/kind, top-k endpoints; nested ignore, symlink/hardlink, protected config, credential content, non-Python supported text, and bounded directory failure. Mutate the original config/exclusion list after the scan opens and verify the effective scan policy and recorded limits stay unchanged. Verify Python and retrieval modes differ only intentionally. Keep existing checked-index tests.

### Acceptance Criteria

- Retrieval has a typed entry contract and strictly admitted text source pool; old index/context defaults and scope behavior unchanged.

### Verification

`uv run --locked pytest -q tests/context/test_retrieval_inputs.py tests/code_index/test_read_generation.py tests/context/test_provider.py` — all pass.

## Task 2: Prepare one bounded lexical pass

### Intent

Use the existing literal search infrastructure as evidence in ranking without scanning or selecting candidates per term.

### Implementation Steps

1. In `retrieval_query.py`, retain the stripped query as a phrase, and extract, in first-seen order, up to 12 identifier, dotted identifier, or relative-path-shaped tokens (using Unicode identifier components, allowing `/`, `.`, `-` only inside path tokens). Keep punctuation-shaped exact selectors without treating them as paths to open. For lexical terms remove V1 stopwords case-insensitively (share the helper/set with `ranking.py`, without modifying `tokenize` behavior); deduplicate preserving case. Split `snake_case`, camel case, and dotted/path components in first-seen order into at most 12 supplemental terms, excluding V1 stopwords, terms shorter than 2 chars and duplicates. An input with no terms still searches the full phrase. Terms with a whole distinct match count at most once per line; matching is case-sensitive fixed substring, not regex or fuzzy matching. Exact symbol matching is defined in Task 4.
2. Add `search_snapshot_literals(lines_by_path, phrase, primary, expanded, *, on_match=None)` in `search.py`. The caller prepares canonical lines once using `iter_lines`; build one stream line table referencing those lines in path/line order. For each matching line, send its path/line, original-term bit mask, expanded-term bit mask and phrase flag to `on_match` exactly once, in canonical order, with no source text. Preserve the union of all masks/phrase flags for file scoring, but retain at most 8 **file** anchors per path, chosen by phrase, descending primary count, descending expanded count, then earliest line. Expose whether file anchors were capped. On the rg path validate the complete output before invoking callbacks; a failed rg run must not produce any evidence before the Python fallback.
3. Use the existing rg executable discovery, no-config/sanitized env/stdin conventions and fixed-string matching. For a nonempty canonical input at most 64 KiB (check encoded size before building the payload), batch phrase and terms in one `rg --no-config --fixed-strings --line-number --no-filename --no-heading --color=never --no-mmap -e <literal> ...` call with stdin only. Output is one numbered line per hit, not one per match; validate exit, strictly increasing mapped line numbers, and canonical line text, then calculate exact flags in Python. Use the V1 timeout and reject stdout over 1 MiB. Nonzero other than no-match, malformed output, timeouts, missing executable: discard *all* partial evidence and use the same Python predicate over the same line table. For larger input use the Python pass deliberately, not one subprocess per term/chunk. Empty input returns no matches without a subprocess. Do not change V1 `_run_rg` or the output of `search()`.

### Required Interfaces and Contracts

- `prepare_query(request)` returns original phrase, exact selector tokens and ordered primary/expanded terms (including cap status); `search_snapshot_literals` never opens files or sees absolute paths. Do not retain the full stream of match metadata for attribution; Task 4 consumes it incrementally.
- Reason flags and result order must be identical regardless of search backend; only a fallback diagnostic may differ. Search text is not included in logs or returned evidence.

### Requirement Evidence

- Original Request 2–3, 9, 10, 12 and token efficiency; reconciliation **Inputs**, **Text evidence**.

### Error Handling and Logging

- DEBUG logs term count, line count, backend, capped flag and hit counts only; WARN on unexpected rg degradation using a stable code, not on deliberate Python selection. No raw argv, task text, stderr, or source lines.

### Tests

- `test_retrieval_search.py`: phrase vs alias, duplicate expansion, Unicode and CRLF mapping, literals beginning with `-` or containing regex metacharacters, large corpus bypass, bounded 8 file anchors but full file coverage and callback coverage; inject malformed rg after valid-looking rows and verify callbacks fire only once via Python. Exercise real rg if installed and fake it for mandatory CI cases.

### Acceptance Criteria

- One prepared line table and one lexical evidence pass per normal request; no persistent FTS, additional packages, per-term file scans or full source emission.

### Verification

`uv run --locked pytest -q tests/context/test_retrieval_search.py tests/context/test_search.py tests/context/test_ranking.py` — all pass.

## Phase Risks and Mitigations

- V1 preview scoping is not an authority check. Keep the new strict scanner and explicit checkout-only query separate.
- Strict lexical admission still temporarily reads bounded file bodies locally to check policy and find evidence; it does not load full files into results or prompts.

## Phase Completion Checklist

- Both tasks meet their acceptance criteria and verification commands pass.
- Update only the task checkboxes in `index.md`.
