"""Reproducibility and oracle contracts for the shared fixture corpus."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks import fixtures as fixtures_mod
from benchmarks.fixtures import (
    CASE_IDS,
    FixtureError,
    apply_intended_edit,
    list_manifests,
    load_manifest,
    materialize,
    run_all_oracles,
    run_oracle,
    weakened_test_variants,
)


def test_seven_cases_are_registered():
    manifests = list_manifests()
    assert [item.id for item in manifests] == list(CASE_IDS)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_materialize_is_reproducible(case_id: str, tmp_path: Path):
    first = materialize(case_id, tmp_path / "a")
    second = materialize(case_id, tmp_path / "b")
    assert first.head == second.head == first.manifest.expected_head
    assert first.head == load_manifest(case_id).expected_head
    status_a = (tmp_path / "a" / ".git").exists()
    status_b = (tmp_path / "b" / ".git").exists()
    assert status_a and status_b
    tracked_a = {
        path.relative_to(first.root).as_posix(): path.read_bytes()
        for path in first.root.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    }
    tracked_b = {
        path.relative_to(second.root).as_posix(): path.read_bytes()
        for path in second.root.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    }
    assert tracked_a == tracked_b


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_oracles_fail_initially_and_pass_on_intended_edit(case_id: str, tmp_path: Path):
    fixture = materialize(case_id, tmp_path / case_id)
    initial = run_all_oracles(fixture.manifest, fixture.root)
    assert initial
    assert all(not result.ok for result in initial), [
        (result.check_id, result.exit_code, result.stdout) for result in initial
    ]

    apply_intended_edit(case_id, fixture.root)
    after = run_all_oracles(fixture.manifest, fixture.root)
    if fixture.manifest.positive_outcome or case_id in {
        "verify_then_repair",
        "budget_exhaustion",
        "insufficient_context",
    }:
        # Intended bytes satisfy the data-only oracle even for negative workflow outcomes.
        assert all(result.ok for result in after), [
            (result.check_id, result.exit_code, result.stdout) for result in after
        ]


def test_test_repair_oracle_rejects_deleted_or_weakened_tests(tmp_path: Path):
    fixture = materialize("test_repair", tmp_path / "repair")
    path = fixture.root / "tests" / "test_mathops.py"
    for label, content in weakened_test_variants().items():
        if content == "":
            path.unlink()
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
        result = run_oracle(fixture.manifest, fixture.root, "assert_test_repair")
        assert not result.ok, label
        assert "E999" in result.stdout


def test_materialize_refuses_head_mismatch(tmp_path: Path, monkeypatch):
    original = fixtures_mod.load_manifest

    def poisoned(case_id: str):
        return replace(original(case_id), expected_head="0" * 40)

    monkeypatch.setattr(fixtures_mod, "load_manifest", poisoned)
    with pytest.raises(FixtureError, match="head_mismatch"):
        fixtures_mod.materialize("bug_fix", tmp_path / "bad")


def test_materialize_ignores_source_pycache_pollution(tmp_path: Path):
    """Bytecode left beside fixture sources must not change the pinned HEAD."""

    tree = fixtures_mod.trees_dir() / "bug_fix" / "src"
    cache = tree / "__pycache__"
    cache.mkdir(exist_ok=True)
    junk = cache / "calc.cpython-314.pyc"
    junk.write_bytes(b"not-a-real-pyc")
    try:
        fixture = materialize("bug_fix", tmp_path / "clean")
        assert fixture.head == load_manifest("bug_fix").expected_head
        tracked = [
            path.relative_to(fixture.root).as_posix()
            for path in fixture.root.rglob("*")
            if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
        ]
        assert all("__pycache__" not in name and not name.endswith(".pyc") for name in tracked)
    finally:
        junk.unlink(missing_ok=True)
        if cache.exists() and not any(cache.iterdir()):
            cache.rmdir()


def test_fixture_trees_have_no_bytecode_inventory():
    """Static guard: checked-in fixture sources must stay free of cache noise."""

    roots = fixtures_mod.trees_dir()
    offenders: list[str] = []
    for path in sorted(roots.rglob("*")):
        relative = path.relative_to(roots)
        if fixtures_mod._is_cache_noise(relative):
            offenders.append(relative.as_posix())
    assert offenders == [], offenders


def test_developer_checkout_is_not_initialized(tmp_path: Path):
    # Materialization must target the disposable path only.
    repo_root = Path(__file__).resolve().parents[2]
    before = (repo_root / ".git" / "HEAD").read_bytes()
    materialize("bug_fix", tmp_path / "isolated")
    assert (repo_root / ".git" / "HEAD").read_bytes() == before
    assert not (repo_root / "src" / "calc.py").exists()
