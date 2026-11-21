# Phase 1: Shared admission and Python facts

Plan: [index.md](index.md)  
Tasks: 1–2  
Depends on: none

## Objective

Expose a strict, bounded way to admit index input and obtain path-independent Python facts from the existing AST parser. Preserve V1 behavior and data shapes.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/context/repository.py` | `Inventory`, `Repository._walk_dir`, `_load_ignore_file`, `_admit`, `FileSnapshot` | Reuse traversal, ignore evaluation, decoding, snapshots, and content digests. `Inventory.complete` alone misses I/O and ignore failures. |
| `src/lean_weave/context/policy.py` | `is_python`, `is_hard_denied*`, `contains_secret` | Existing Python suffix and quarantine rules; a canary filter is not a guarantee against arbitrary secrets. |
| `src/lean_weave/executor/workspace.py` | `Workspace._read_bytes`, `discover` | Descriptor-relative no-follow reads and explicit incomplete-result semantics; tool approval policy is not the index policy. |
| `src/lean_weave/context/python_index.py` | `parse_module`, `_collect`, `_raw_imports`, `_resolve_imports`, `build_python_index` | One parser and resolver; V1 import targets intentionally use path-only inventory. |
| `src/lean_weave/context/search.py` | `search` | Bounded literal search using admitted text on rg stdin, with Python fallback. Not a definition parser. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/context/repository.py` | Modify | Strict admission mode of existing `Repository`, with separate completeness/outcome view. |
| `src/lean_weave/context/python_index.py` | Modify | Split extraction and projection; keep existing public V1 entry points. |
| `tests/context/test_repository.py`, `tests/context/test_python_index.py` | Extend | New strict/AST contracts and unchanged legacy behavior. |

## Task 1: Add strict source admission for index maintenance

### Intent

Prevent uncertain scans from being published as valid revisions, without making legacy context previews fail on incomplete repositories.

### Implementation Steps

1. Add opt-in `strict_source: bool = False` to `Repository`. Use its existing walk and admission control flow, not a second inventory crawler. In strict mode anchor the root with a directory descriptor; list/stat descendants and open ignore/source files relative to checked descriptors. Use no-follow opens, nonblocking regular-file admission, before/after identity checks (device, inode, size, mtime, ctime, link count), and a bounded read loop. Reject hardlinked sources and ignore files. Verify the installed path still refers to the read file, and classify detected replacement as uncertainty. Use context-managed descriptors with closure on every path. If required OS primitives are unavailable, report strict admission unavailable rather than falling back to pathname checks.
2. Keep existing lexically sorted walk, nested `.gitignore` and additive `context.exclusions`, state/cache/generated hard denies and nested-repository pruning. Strict mode marks invalid, oversized, unreadable, symlinked or otherwise indeterminate `.gitignore` as incomplete and prunes that subtree; a genuinely absent ignore file is normal. Track directory/stat/read uncertainty and work caps separately from deliberate omissions. Run credential quarantine before the caller receives any source digest, path record, or AST facts.
3. Add `scan_python_sources()` on strict `Repository`: enumerate existing inventory `.py`/`.pyi`, read each through the strict admission path, and return a frozen `PythonSourceScan` with sorted admitted snapshots, `complete`, and aggregate omission counts keyed by safe reason. Add a shared `validate_index_source_path()` in `context/repository.py`: require the existing relative-path normalizer to round-trip unchanged, the Python suffix to match, and strict UTF-8 without ASCII controls, a drive prefix or credential canaries. Use the same predicate in lookup and SQLite hydration; skip rejected discovered names without logging them. Add an optional caller-owned `protected_paths` tuple of normalized paths (CLI supplies any in-root selected product config path); reject these before reading. Share `ContextConfig` candidate/scan/entry bounds. Only snapshots from this method reach the persistent writer.
4. In strict `_walk_dir`, collect at most the remaining entry budget plus one from `os.scandir(dir_fd)` before sorting. If the extra entry is seen, mark incomplete and do not process that directory’s partial batch. Count every enumerated entry, not just accepted files; bound descent at 128 directory levels and fail incomplete on exhaustion. Check nested `.git` with descriptor-relative no-follow stat; any present marker prunes, and a failed probe other than absence is incomplete. Charge bytes actually read, including ignore and ultimately rejected bodies; at zero remaining budget, a required nonempty read is incomplete. Use pre-open size for a known oversized source omission; growth or changed identity during a read is uncertainty, not a deliberate exclusion.
5. Keep a bounded scan witness of observed root/directory identities, checked Python candidate identities (including stat-based omissions), and each processed directory’s `.gitignore` presence and identity. Sample directory identity before examining its contents, so a formerly absent ignore file appearing mid-scan can be detected. Make strict scans a context-managed session whose anchored descriptor stays open through the caller’s final witness check, without retaining a descriptor per visited directory. `scan.revalidate()` walks witnesses descriptor-relative with no-follow and reports false on changed/disappeared observations or non-absence errors; it never rereads bodies. Compare the live root to the held descriptor. A writer and any reader claiming `current` must use this check.

### Required Interfaces and Contracts

- `with repository.scan_python_sources() as scan` yields `PythonSourceScan` with `.snapshots: tuple[FileSnapshot, ...]`, `.complete: bool`, `.omission_counts: Mapping[str, int]`, and `.revalidate() -> bool`. Absent, deliberately excluded, unsupported, quarantined, oversized candidate, invalid encoding and binary are normal omissions; entry/depth/aggregate-byte exhaustion, indeterminate policy, vanished-after-enumeration paths, and I/O or identity races prevent publication. Invalid syntax is not a source admission error.
- Reuse `decode_snapshot`, canonical newline normalization and `FileSnapshot.digest`; changes to raw bytes that leave canonical text unchanged do not require AST parsing. No global Git ignores, tracked-file filtering, source execution or extension expansion. Existing non-strict methods and omission vocabulary retain their public shapes.

### Requirement Evidence

Request 3–4 and 8–14; reconciliation rows **Admitted corpus**, **Partial scan**. `.ai-factory/ARCHITECTURE.md` — context policy; V1-04 Requirements Reconciliation — gated reads.

### Error Handling and Logging

DEBUG aggregate candidates, admitted and skipped counts; WARN safe reason when incomplete; redact raw filenames for rejections, credentials, config values, source text, and raw exceptions. Keep V1 logging behavior outside strict mode.

### Tests

Extend `tests/context/test_repository.py`: complete empty repository, nested rules and negation precedence, additive exclusions, generated artifact and protected config, unsupported/binary inputs, noncanonical filenames, credential canary absent from snapshots/diagnostics, outside-root selectors, symlink and hardlink cases, and descriptor-relative ancestor swap. Check that a known oversized source is omitted but an oversized/unreadable ignore is incomplete, a wide directory stops consuming its iterator at budget plus one, and depth/byte exhaustion are incomplete. Change a file and create a previously absent `.gitignore` after a successful scan and assert revalidation fails. Do not infer removals from any uncertainty.

### Acceptance Criteria

Strict scans return only policy-admitted canonical Python snapshots and distinguish a complete empty corpus from uncertainty. Existing repository preview tests still pass.

### Verification

`uv run --locked pytest tests/context/test_repository.py tests/executor/test_workspace.py` — both admission suites pass.

## Task 2: Share path-independent Python extraction

### Intent

Parse each changed source once, let an unambiguous move reuse facts, and keep executor and context consumers on the same AST code.

### Implementation Steps

1. Split `parse_module` in `context/python_index.py` into `extract_python_facts(snapshot)` and `project_python_facts(path, facts, *, module_paths=None)`. Make `parse_module` call both. Refactor the existing collector and import walk rather than adding a parallel parser. Extract local qualified name, simple name, kind, parent local name, existing AST-unparsed signature, decorators in source order (AST-unparsed, without `@`), and inclusive 1-based decorated definition range. Store imports as per-statement records: plain/from kind, module or null, relative level, ordered `(name, alias-or-null)` entries, inclusive start/end lines. Preserve source order by line and original traversal ordinal. Derive module name and module range (`1..line_count`, null for an empty file) at projection/read time; no full source body is kept in facts.
2. Project V1 `ParsedModule` and `SymbolRef` exactly as before. Adapt import records back to the resolver’s existing input; recompute edges from the current path and provided module table, not persistent resolved edges. Keep syntax/parse-limit effects compatible with V1; catch bounded AST recursion/value failures without raw error text; do not swallow database or I/O exceptions here.
3. Define typed frozen `PythonFacts`, `DefinitionFact`, and `ImportFact` beside the existing parser. Establish `EXTRACTOR_VERSION = 1` and an extractor key including the Python implementation and major/minor version; bump the explicit version when parsing/metadata semantics change. V2 serialization lives in the store, not in V1 `SymbolRef` or `ContextPack`. Keep `search.search()` unchanged for V1 lexical lookup; SQLite exact symbol reads need neither rg nor FTS.

### Required Interfaces and Contracts

- `extract_python_facts(snapshot: FileSnapshot) -> PythonFacts` with `status` one of `ok`, `syntax`, `parse_limit`; failed facts have no definitions or imports. `project_python_facts(...) -> ParsedModule`; map failed V2 outcomes to the existing V1 `error="syntax"` representation.
- Definition occurrence ordinal is zero-based among `(local_qualified_name, kind)` ordered by `(start_line, end_line, traversal_order)`; duplicate definitions remain distinct. Lines count decoded text lines, not byte offsets. Nested class/method and async classifications follow V1. A rename changes path-qualified names/import resolution but not path-independent facts.

### Requirement Evidence

Request 5–7, 10 and token-efficiency clauses; reconciliation rows **Fresh and invalid Python**, **Move or duplicate content**. V1-04 — existing symbol and source range contract; project rules — prefer deterministic operations.

### Error Handling and Logging

DEBUG parse outcome and fact counts; never log source, decorators, signatures, import text, identifiers, or raw parser exceptions in new paths.

### Tests

Extend `tests/context/test_python_index.py` for multiline decorators, positional-only/keyword-only and async signatures, Unicode identifiers, `.pyi`, nested definitions, duplicates, relative/wildcard/aliased imports, empty and broken files, and move projection under a different package. Assert serialized V1 references and existing resolver behavior are unchanged.

### Acceptance Criteria

One `ast.parse` per extraction, correct decorator-inclusive source ranges, no fabricated definitions on syntax failure, and unchanged V1 JSON fields.

### Verification

`uv run --locked pytest tests/context/test_python_index.py tests/context/test_search.py tests/executor/test_source.py` — V1 and added cases pass.

## Phase Risks and Mitigations

- Strict TOCTOU handling could affect V1: gate it explicitly, reuse the same policy and snapshot conversion, and keep the default unchanged.
- Fact enrichment could change V1 ranking or prompts: project only the legacy fields and assert existing outputs.

## Phase Completion Checklist

- Tasks 1–2 meet their acceptance criteria and verification passes.
- Update their checkboxes only in `index.md`.
