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


def test_schedule_is_declared_and_order_alternates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    run = run_matrix(
        {
            "cases": ["bug_fix"],
            "variants": ["v1", "v2"],
            "repeats": 2,
        }
    )
    schedule = run.spec["schedule"]
    assert [(entry["variant"], entry["repetition"]) for entry in schedule] == [
        ("v1", 1),
        ("v2", 1),
        ("v2", 2),
        ("v1", 2),
    ]


def test_duplicate_selectors_are_rejected_before_execution():
    from lean_weave.benchmark.runner import run_matrix

    for spec in (
        {"cases": ["bug_fix", "bug_fix"], "variants": ["v1"]},
        {"cases": ["bug_fix"], "variants": ["v1", "v1"]},
    ):
        try:
            run_matrix(spec)
        except ValueError as exc:
            assert "duplicate" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("duplicate selector accepted")


def test_conditions_match_for_pair_and_capture_effective_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    run = run_matrix(
        {"cases": ["bug_fix"], "variants": ["v1", "v2"], "repeats": 1}
    )
    v1, v2 = run.rows
    assert v1.conditions["fixture"] == v2.conditions["fixture"]
    assert v1.conditions["verifier"]["digest"] == v2.conditions["verifier"]["digest"]
    assert v1.conditions["runtime"] == v2.conditions["runtime"]
    assert v1.conditions["context_mode"] != v2.conditions["context_mode"]
    assert v1.conditions["digest"] != v2.conditions["digest"]
    assert v1.fixture_fingerprint == v2.fixture_fingerprint
    assert v1.evidence["dispatch"] == "observed"


def test_fixture_setup_failure_retains_arm_row(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    from lean_weave.benchmark import runner

    def boom(*args, **kwargs):
        raise RuntimeError("materialize_failed")

    monkeypatch.setattr(runner, "prepare_case", boom)
    run = runner.run_matrix(
        {"cases": ["bug_fix"], "variants": ["v1"], "repeats": 1}
    )
    assert run.incomplete is True
    assert len(run.rows) == 1
    assert run.rows[0].status == "error"
    assert run.rows[0].error_class == "RuntimeError"


def test_condition_capture_reacts_to_model_and_verifier_changes(tmp_path, monkeypatch):
    import json
    import yaml
    from dataclasses import replace

    from lean_weave.benchmark.conditions import SOURCE_ROOT, capture_arm_conditions
    from lean_weave.benchmark.fixtures import load_manifest
    from lean_weave.benchmark.offline import prepare_case

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    case = prepare_case("bug_fix", tmp_path / "case")
    manifest = load_manifest("bug_fix")

    def capture(manifest_item, config_path):
        return capture_arm_conditions(
            manifest=manifest_item,
            origin_head=manifest_item.expected_head,
            variant_name="v1",
            context_mode="v1",
            context_extra={},
            config_path=config_path,
        )

    baseline = capture(manifest, case.config_path)
    assert str(SOURCE_ROOT) not in json.dumps(baseline)
    assert baseline["fixture"]["pinned_head"] == manifest.expected_head

    payload = yaml.safe_load(case.config_path.read_text())
    payload["llm"]["model"] = "deepseek-reasoner"
    altered_config = tmp_path / "altered.yaml"
    altered_config.write_text(yaml.safe_dump(payload))
    altered_model = capture(manifest, altered_config)
    assert altered_model["config"]["digest"] != baseline["config"]["digest"]
    assert altered_model["digest"] != baseline["digest"]

    altered_manifest = replace(
        manifest,
        checks=(
            replace(manifest.checks[0], timeout_s=manifest.checks[0].timeout_s + 1),
        ),
    )
    altered_verifier = capture(altered_manifest, case.config_path)
    assert (
        altered_verifier["verifier"]["digest"]
        != baseline["verifier"]["digest"]
    )


def test_release_binding_records_supplied_wheel_hash(tmp_path, monkeypatch):
    from lean_weave.benchmark import conditions
    from lean_weave.benchmark.conditions import release_binding

    wheel = tmp_path / "pkg-0.1-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    monkeypatch.setenv("LEAN_WEAVE_BENCHMARK_WHEEL", str(wheel))
    monkeypatch.setattr(conditions, "code_tree_dirty", lambda: False)
    binding = release_binding()
    assert binding["wheel_sha256"].startswith("sha256:")
    assert "wheel_hash_unavailable" not in binding["reasons"]
    # Environment wheel hashes remain observational; formal eligibility needs
    # the verified artifact-binding protocol.
    assert binding["release_eligible"] is False
    assert "formal_binding_required" in binding["reasons"]
    assert "source_tree_dirty" not in binding["reasons"]
