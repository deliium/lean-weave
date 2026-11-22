from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from lean_weave.cli.app import app


def test_run_report_compare_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    runner = CliRunner()
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    first = runner.invoke(
        app,
        [
            "benchmark",
            "run",
            "--suite",
            "workflow",
            "--case",
            "bug_fix",
            "--variant",
            "v1",
            "--output",
            str(baseline),
        ],
    )
    assert first.exit_code == 0, first.stdout
    payload = json.loads(baseline.read_text())
    assert payload["rows"][0]["status"] == "success"
    assert payload["evidence_source"] == "offline_scripted"

    second = runner.invoke(
        app,
        [
            "benchmark",
            "run",
            "--suite",
            "workflow",
            "--case",
            "bug_fix",
            "--variant",
            "v2",
            "--output",
            str(candidate),
        ],
    )
    assert second.exit_code == 0, second.stdout
    comparison = runner.invoke(
        app,
        [
            "benchmark",
            "compare",
            str(baseline),
            str(candidate),
            "--axis",
            "strategy",
            "--json",
        ],
    )
    assert comparison.exit_code == 0, comparison.stdout
    compared = json.loads(comparison.stdout.strip().splitlines()[-1])
    assert compared["comparable"] is True
    assert compared["pairs"] == 1
    report = runner.invoke(app, ["benchmark", "report", str(baseline)])
    assert report.exit_code == 0
    assert "# Benchmark run" in report.stdout


def test_existing_output_is_not_clobbered(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    output = tmp_path / "exists.json"
    output.write_text("sentinel")
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "--suite",
            "workflow",
            "--case",
            "bug_fix",
            "--variant",
            "v1",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code != 0
    assert output.read_text() == "sentinel"


def test_all_suite_rejects_filters(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "--suite",
            "all",
            "--case",
            "bug_fix",
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    assert result.exit_code != 0


def test_paired_live_selection_is_validated_before_launch(tmp_path):
    runner = CliRunner()
    output = tmp_path / "live.json"
    # Only exactly v1+v2 is a valid paired live opt-in.
    invalid = runner.invoke(
        app,
        [
            "benchmark",
            "run",
            "--live",
            "--suite",
            "workflow",
            "--case",
            "bug_fix",
            "--variant",
            "v2",
            "--output",
            str(output),
        ],
    )
    assert invalid.exit_code != 0
    assert not output.exists()

    # A valid paired selection proceeds to admission and fails closed without
    # operator config/limits/credentials; it never launches a child.
    paired = runner.invoke(
        app,
        [
            "benchmark",
            "run",
            "--live",
            "--suite",
            "workflow",
            "--case",
            "bug_fix",
            "--variant",
            "v1",
            "--variant",
            "v2",
            "--output",
            str(output),
        ],
    )
    assert paired.exit_code != 0
    assert not output.exists()
