"""Installed benchmark commands: run, compare, report."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Optional

import typer

from lean_weave.benchmark.compare import (
    ComparisonError,
    compare_runs,
    load_run,
    load_thresholds,
)
from lean_weave.benchmark.models import ArmRow, BenchmarkRun
from lean_weave.benchmark.report import (
    render_comparison_markdown,
    render_run_json,
    render_run_markdown,
)
from lean_weave.errors import ConfigError
from lean_weave.logging_setup import get_logger

log = get_logger("cli.benchmark")

benchmark_app = typer.Typer(help="Run, compare and report deterministic benchmarks.")
MAX_REPEATS = 10


def _reject_existing(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ConfigError("benchmark_output_exists")


def _publish(path: Path, data: str) -> None:
    """Exclusively create ``path`` via temp sibling + hard link (no replace)."""

    parent = path.parent
    if not parent.exists():
        raise ConfigError("benchmark_output_parent_missing")
    if path.exists() or path.is_symlink():
        raise ConfigError("benchmark_output_exists")
    temp = parent / f".lw-bench-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            encoded = data.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            raise ConfigError("benchmark_output_exists") from None
        except OSError:
            raise ConfigError("benchmark_output_publish_failed") from None
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise ConfigError("benchmark_output_publish_failed")
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _write_output(path: Path, payload: str) -> None:
    _publish(path, payload)
    log.info("[CLI.benchmark] wrote output=%s", path.name)


@benchmark_app.command("run")
def benchmark_run(
    suite: str = typer.Option("workflow", "--suite"),
    cases: Optional[list[str]] = typer.Option(None, "--case"),
    variants: Optional[list[str]] = typer.Option(None, "--variant"),
    repeats: int = typer.Option(1, "--repeats", min=1, max=MAX_REPEATS),
    output: Path = typer.Option(..., "--output"),
    live: bool = typer.Option(False, "--live"),
    live_config_dir: Optional[Path] = typer.Option(None, "--live-config-dir"),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens"),
    max_cost_usd: Optional[str] = typer.Option(None, "--max-cost-usd"),
    pricing: Optional[Path] = typer.Option(None, "--pricing"),
    deadline_s: Optional[float] = typer.Option(None, "--deadline-s"),
) -> None:
    """Run a pinned offline suite; live mode requires explicit limits."""

    if suite not in {"workflow", "retrieval", "all"}:
        raise ConfigError("benchmark_suite_invalid")
    if suite == "all" and (cases or variants):
        raise ConfigError("benchmark_all_filters_forbidden")
    if live and suite != "workflow":
        raise ConfigError("benchmark_live_requires_workflow")
    _reject_existing(output)
    spec = {
        "cases": list(cases or ()),
        "variants": list(variants or ()),
        "repeats": repeats,
    }
    if live:
        # Local import avoids the CLI <-> benchmark-runner import cycle.
        from lean_weave.benchmark.live import validate_live_request

        admission = validate_live_request(
            live=True,
            cases=cases or ["bug_fix"],
            config_dir=live_config_dir,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            pricing_path=pricing,
            deadline_s=deadline_s,
            repeats=repeats,
        )
        log.info(
            "[CLI.benchmark] live_admission cases=%s tokens=%s",
            len(admission.cases),
            admission.reserved_tokens,
        )
        # Local import avoids the CLI <-> benchmark-runner import cycle.
        from lean_weave.benchmark.runner import run_live_matrix

        live_run = run_live_matrix(
            {
                "cases": admission.cases,
                "repeats": admission.repeats,
                "config_paths": [str(path) for path in admission.config_paths],
                "max_tokens": max_tokens,
                "max_cost_usd": str(max_cost_usd),
                "deadline_s": deadline_s,
            }
        )
        payload = render_run_json(live_run)
        _write_output(output, payload)
        typer.echo(payload, nl=False)
        if live_run.incomplete:
            raise typer.Exit(1)
        return
    # Local import avoids the CLI <-> benchmark-runner import cycle.
    from lean_weave.benchmark.runner import run_matrix, run_retrieval_matrix

    if suite == "workflow":
        run = run_matrix(spec)
    elif suite == "retrieval":
        run = run_retrieval_matrix(spec)
    else:
        workflow = run_matrix(spec)
        retrieval = run_retrieval_matrix(spec)
        combined = BenchmarkRun(
            schema_version=workflow.schema_version,
            evidence_source="offline_scripted",
            suite="all",
            started_at=workflow.started_at,
            completed_at=retrieval.completed_at,
            revision=workflow.revision,
            spec={"workflow": workflow.spec, "retrieval": retrieval.spec},
            condition_fingerprint=workflow.condition_fingerprint,
            rows=[*workflow.rows, *retrieval.rows],
            incomplete=workflow.incomplete or retrieval.incomplete,
            notes=["retrieval rows carry local_estimate provenance"],
        )
        run = combined
    payload = render_run_json(run)
    _write_output(output, payload)
    typer.echo(payload, nl=False)


@benchmark_app.command("compare")
def benchmark_compare(
    baseline: Path,
    candidate: Path,
    axis: str = typer.Option("strategy", "--axis"),
    thresholds: Optional[Path] = typer.Option(None, "--thresholds"),
    baseline_variant: Optional[str] = typer.Option(None, "--baseline-variant"),
    candidate_variant: Optional[str] = typer.Option(None, "--candidate-variant"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two versioned results within one named axis."""

    try:
        baseline_payload = load_run(baseline)
        candidate_payload = load_run(candidate)
        gate_policy = load_thresholds(thresholds) if thresholds is not None else None
        comparison = compare_runs(
            baseline_payload,
            candidate_payload,
            axis=axis,
            baseline_variant=baseline_variant,
            candidate_variant=candidate_variant,
            thresholds=gate_policy,
        )
    except ComparisonError as exc:
        log.warning("[CLI.benchmark] compare outcome=invalid code=%s", exc)
        raise ConfigError(str(exc)) from None
    payload = comparison.to_dict()
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        typer.echo(render_comparison_markdown(payload), nl=False)
    if comparison.failed_gates or comparison.unevaluable_gates:
        raise typer.Exit(1)
    if not comparison.comparable:
        raise typer.Exit(2)
    if any(row.get("correct") is False for row in baseline_payload.get("rows", [])):
        raise typer.Exit(1)
    if any(row.get("correct") is False for row in candidate_payload.get("rows", [])):
        raise typer.Exit(1)


@benchmark_app.command("report")
def benchmark_report(
    result: Path,
    output: Optional[Path] = typer.Option(None, "--output"),
) -> None:
    """Render a human-readable Markdown report from one run result."""

    try:
        payload = load_run(result)
    except ComparisonError as exc:
        raise ConfigError(str(exc)) from None
    run = BenchmarkRun(
        schema_version=payload["schema_version"],
        evidence_source=payload["evidence_source"],
        suite=payload["suite"],
        started_at=payload["started_at"],
        completed_at=payload.get("completed_at"),
        revision=payload.get("revision"),
        spec=payload.get("spec") or {},
        condition_fingerprint=payload.get("condition_fingerprint"),
        rows=[],
    )
    run.rows = [ArmRow(**row) for row in payload["rows"]]
    run.incomplete = bool(payload.get("incomplete"))
    run.notes = list(payload.get("notes") or [])
    rendered = render_run_markdown(run)
    if output is not None:
        _reject_existing(output)
        _write_output(output, rendered)
        typer.echo(f"report={output}")
    else:
        typer.echo(rendered, nl=False)
