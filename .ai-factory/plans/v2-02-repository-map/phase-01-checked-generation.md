# Phase 1: Checked source generation

Plan: [index.md](index.md)  
Tasks: 1  
Depends on: completed `v2-01-code-indexer`

## Objective

Expose the admitted, immutable index generation without a second database, source parser, or change to existing lookup behavior.

## Current-Code Evidence

| Path | Symbols | Why it matters |
|---|---|---|
| `src/lean_weave/code_index/index.py` | `CodeIndex.lookup`, `_read_lease`, `_policy_key`, `scoped_facts` | Lookup already checks one generation and revalidates live admission; scoped facts alone cannot prove graph completeness. |
| `src/lean_weave/code_index/models.py` | `IndexHeader`, `FileRecord`, `source_revision` | Records contain path, module, file ID, digest, line count, status and immutable facts. Revision does not include the extractor key. |
| `src/lean_weave/code_index/sqlite_store.py` | `SqliteIndexStore.inspect` | Hydrates and validates ordered records, manifest, schema and checkout identity. |
| `src/lean_weave/context/repository.py` | `Repository.scan_python_sources` | Strict scan admits live source and provides completeness and witness verification. |

## Files to Change

| Path | Action | Required change |
|---|---|---|
| `src/lean_weave/code_index/{models,index,__init__}.py` | modify | Export a checked generation and share lookup's checked-read sequence. |
| `tests/code_index/test_read_generation.py` | create | Whole-generation validation, freshness and no-reparse tests. |

## Task 1: Export checked indexed facts

### Intent

Provide a supported input to the reusable graph with the same safety and freshness boundary as exact lookup.

### Implementation Steps

1. Add frozen `IndexGeneration(header: IndexHeader, records: tuple[FileRecord, ...])` in `models.py` and export the type from `code_index/__init__.py`.
2. Extract `lookup`'s existing `location → read lease → inspect → strict live scan → currentness check → final witness check` into private context manager `CodeIndex._checked_generation()` yielding `IndexGeneration` inside both contexts. Keep the existing checks and their ordering; revalidate on normal exit before returning results, including empty results. Lookup validates selectors and limits first, then collects hits inside the context, keeping its return and error contract.
3. Implement `CodeIndex.read_generation() -> IndexGeneration` by returning the checked value after context exit. No `status()` preflight, raw SQLite access by consumers, scope widening, or fallback parsing. No schema, extractor, migration or index-update change.

### Required Interfaces and Contracts

- Records are immutable, path ordered, and from one compatible, healthy, freshly checked generation. Types and facts are existing `DefinitionFact` and `ImportFact`; no source bodies are returned. Cached syntax/parse-limit records retain metadata and do not trigger AST work.
- “Current” means observed content and policy at read verification, not a promise about writes after lease/witness release. Cache/API reads validate live source by hashing; no AST extraction, workspace writes, network, or index rebuild.

### Requirement Evidence

- Original Request architecture and requirements 13, 15–16; reconciliation checked lifecycle and representative fixture rules. V2-01 Tasks 5–7 define freshness, incremental reconciliation and scoped read boundaries.

### Error Handling and Logging

- Preserve lookup's codes (`missing`, `stale`, `incomplete`, storage/lease errors); changing witnesses fail as today. No partial results or old-generation fallback. `DEBUG [CodeIndex.read_generation]` may log outcome and count only after verification; no paths, contents, raw store errors or new duplicate user-facing logs.

### Tests

- Add `tests/code_index/test_read_generation.py` using index fixtures: healthy ordered immutable tuples including `.pyi` and syntax failure; valid empty generation; fail-fast `code_index.index.extract_python_facts` tripwire; stale edit or policy/extractor drift; busy, corruption, incomplete scan and final witness failure. Assert writes are unchanged and shared lookup still refuses these failures.

### Acceptance Criteria

- Healthy cache yields one checked revision and facts without AST work. Invalid/stale/unstable generations never produce records; existing lookup results and errors remain unchanged.

### Verification

- `uv run --locked pytest tests/code_index/test_read_generation.py tests/code_index/test_lookup.py tests/code_index/test_incremental.py` — checked reads and existing incremental behavior pass.

## Phase Risks and Mitigations

- Risk: check-then-read race or bypass of source admission. Mitigation: maintain one read lease and scan lifecycle around hydration and checked read; keep graph construction outside that lifecycle and label the snapshot guarantee accurately.

## Phase Completion Checklist

- Task 1 meets acceptance criteria and verification passes.
- Update only the Task 1 checkbox in `index.md`.
