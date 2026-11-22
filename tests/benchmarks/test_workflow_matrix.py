from __future__ import annotations

from lean_weave.benchmark.runner import run_matrix, run_retrieval_matrix


def test_matched_workflow_arms_and_repair_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    run = run_matrix(
        {
            "cases": ["bug_fix", "verify_then_repair"],
            "variants": ["v1", "v2"],
            "repeats": 1,
        }
    )
    assert run.incomplete is False
    assert len(run.rows) == 4
    for row in run.rows:
        assert row.correct is True, row
        assert row.status == "success"
        assert row.total_tokens is not None
        assert row.coverage["total_tokens"] is True
    repairs = [row for row in run.rows if row.case_id == "verify_then_repair"]
    assert all(row.workflow_retries == 1 for row in repairs)
    assert all(row.verification_status == "pass" for row in repairs)


def test_unknown_case_is_rejected_before_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    try:
        run_matrix({"cases": ["missing"], "variants": ["v1"], "repeats": 1})
    except ValueError as exc:
        assert "unknown_case" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown case accepted")


def test_repeat_runs_share_condition_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    first = run_matrix({"cases": ["bug_fix"], "variants": ["v1"], "repeats": 1})
    second = run_matrix({"cases": ["bug_fix"], "variants": ["v1"], "repeats": 1})
    assert first.condition_fingerprint == second.condition_fingerprint
    assert first.rows[0].origin_head == second.rows[0].origin_head


def test_retrieval_matrix_records_local_estimate_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    run = run_retrieval_matrix(
        {"cases": ["multi_file_banner"], "variants": ["v1_default", "v2_default"]}
    )
    assert run.evidence_source == "local_estimate"
    assert len(run.rows) == 2
    assert all(row.retrieval["provenance"] == "local_estimate" for row in run.rows)
