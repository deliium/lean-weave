"""Matched offline benchmark runner over the public FeatureSession."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2

from lean_weave.benchmark.fixtures import (
    CASE_IDS,
    intended_edit_files,
    load_manifest,
    materialize,
    run_all_oracles,
)
from lean_weave.benchmark.models import (
    BENCHMARK_SCHEMA_VERSION,
    ArmRow,
    BenchmarkRun,
    WORKFLOW_VARIANT_NAMES,
    workflow_variant,
)
from lean_weave.benchmark.offline import (
    ContentShadow,
    ScriptedTransport,
    final_completion,
    out_of_policy_plan_script,
    plan_completion,
    prepare_case,
    success_script,
    tool_completion,
    worktree_for,
)
from lean_weave.benchmark.retrieval import evaluate_retrieval
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.analytics import aggregate_usage
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.workflow.session import FeatureSession
from lean_weave.workflow.state import JsonCheckpointStore

log = get_logger("benchmark.runner")

MAX_REPEATS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _condition_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _repair_script(manifest, shadow: ContentShadow) -> list[Any]:
    """Approved wrong edit, failed verification, one repair, passing check."""

    edits = intended_edit_files(manifest.id)
    relative = manifest.write_paths[0]
    correct = edits[relative]
    wrong = correct.replace("= 2", "= 3") if "= 2" in correct else correct + "\n# wrong\n"
    steps: list[Any] = [plan_completion(manifest)]
    steps.append(tool_completion(shadow, relative, wrong, call_id="r1"))
    steps.append(final_completion("first-attempt"))
    steps.append(tool_completion(shadow, relative, correct, call_id="r2"))
    steps.append(final_completion("repaired"))
    return steps


def _script_for(case_id: str, case, shadow: ContentShadow) -> list[Any]:
    expected = str(case.manifest.expected_behavior.get("outcome", "success"))
    if expected == "success_after_repair":
        return _repair_script(case.manifest, shadow)
    if expected not in {"success"} and case_id == "insufficient_context":
        return out_of_policy_plan_script(case.manifest)
    if expected not in {"success"} and case_id == "budget_exhaustion":
        return [plan_completion(case.manifest)]
    return success_script(case.manifest, shadow)


def _negative_matches(case_id: str, expected: str, status: str, reason: str) -> bool:
    if expected == "budget_exhausted":
        return status in {"budget_exhausted", "failed"}
    if expected == "failed":
        if case_id == "insufficient_context":
            return status in {"failed", "interrupted"} and (
                "plan" in reason or "context" in reason or not reason
            )
        return status in {"failed", "interrupted"}
    return False


def run_offline_arm(
    case_id: str,
    variant_name: str,
    *,
    repetition: int = 1,
) -> ArmRow:
    variant = workflow_variant(variant_name)
    arm_id = f"{case_id}:{variant.name}:r{repetition}"
    started = time.monotonic()
    scratch = Path(tempfile.mkdtemp(prefix="lw-arm-"))
    row = ArmRow(
        arm_id=arm_id,
        suite="workflow",
        case_id=case_id,
        variant=variant.name,
        repetition=repetition,
        expected_outcome="",
        provenance="offline_scripted",
        conditions=variant.conditions(),
    )
    try:
        case = prepare_case(
            case_id,
            scratch,
            context_mode=variant.context_mode,
            context_extra=variant.context_extra or None,
        )
        manifest = case.manifest
        expected = str(manifest.expected_behavior.get("outcome", "success"))
        row.expected_outcome = expected
        row.expected_negative = expected not in {"success", "success_after_repair"}
        row.fixture_fingerprint = f"sha256:{manifest.expected_head}"
        row.origin_head = case.origin_head if hasattr(case, "origin_head") else manifest.expected_head
        shadow = ContentShadow.from_tree(case.origin)
        transport = ScriptedTransport(_script_for(case_id, case, shadow))
        session = FeatureSession(source_cwd=case.origin)
        view = session.start(
            manifest.request,
            mode="auto",
            config_path=case.config_path,
            transport=httpx2.MockTransport(transport.handler),
        )
        row.status = view.status
        row.observed_outcome = view.status
        row.reason = view.reason
        row.feature_id = view.feature_id
        row.duration_ms = int((time.monotonic() - started) * 1000)
        worktree = worktree_for(case.origin, view.feature_id)
        journal = _journal(worktree)
        store = JsonCheckpointStore(worktree)
        snapshot = journal.reporting_snapshot(view.feature_id)
        report = aggregate_usage(snapshot)
        totals = report.totals
        if totals is not None:
            row.input_tokens = totals.input_tokens
            row.output_tokens = totals.output_tokens
            row.total_tokens = totals.total_tokens
            row.cache_hit_tokens = totals.cache_hit_tokens
            row.reasoning_tokens = totals.reasoning_tokens
            row.budget_charge = totals.budget_charge
            row.logical_calls = totals.logical_calls
            row.retry_attempts = totals.retry_attempts
            row.failed_attempts = totals.failed_attempts
            row.unresolved_attempts = totals.unresolved_attempts
            row.coverage = totals.coverage.complete()
        estimated = [
            item.estimated_input_tokens
            for item in snapshot.attempts
            if item.estimated_input_tokens is not None
        ]
        row.estimated_input_tokens = sum(estimated) if estimated else None
        envelope = store.load_checkpoint(view.feature_id)
        row.workflow_retries = envelope.state.retry_count
        row.active_ms = envelope.control.active_ms_total
        row.verified_tasks = sum(
            1
            for task in (envelope.state.plan.tasks if envelope.state.plan else [])
            if task.status.value == "done"
        )
        task_tokens = [
            item.measured_total
            for item in snapshot.attempts
            if item.task_id is not None
            and item.stage in {"implement", "retry"}
            and item.measured_total is not None
        ]
        row.task_tokens = sum(task_tokens) if task_tokens else None
        verification = envelope.state.verification
        if verification is not None:
            row.verification_status = verification.status.value
            row.verification_blocking = verification.blocking
        row.context = {
            "mode": variant.context_mode,
            "available": bool(envelope.control.closed_attempts),
            "attempts": len(envelope.control.closed_attempts),
        }
        if row.expected_negative:
            row.correct = _negative_matches(case_id, expected, view.status, view.reason)
            row.oracle_ok = None
        else:
            oracle = all(item.ok for item in run_all_oracles(manifest, worktree))
            row.oracle_ok = oracle
            minimum_retries = int(manifest.expected_behavior.get("min_retry_count", 0) or 0)
            row.correct = bool(
                view.status == "success"
                and oracle
                and (row.workflow_retries or 0) >= minimum_retries
            )
        if row.correct is False:
            row.reason = row.reason or "outcome_mismatch"
        log.info(
            "[Benchmark.arm] arm=%s status=%s correct=%s tokens=%s retries=%s",
            arm_id,
            row.status,
            row.correct,
            row.total_tokens,
            row.workflow_retries,
        )
        return row
    except Exception as exc:  # noqa: BLE001 - infrastructure failures are evidence
        row.status = "error"
        row.correct = False
        row.error_class = type(exc).__name__
        row.reason = row.reason or "arm_error"
        row.duration_ms = int((time.monotonic() - started) * 1000)
        log.warning("[Benchmark.arm] arm=%s outcome=error class=%s", arm_id, row.error_class)
        return row
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _journal(worktree: Path) -> JsonlMetricsCollector:
    return JsonlMetricsCollector((worktree / ".lean-weave").resolve())


def run_matrix(spec: Mapping[str, Any]) -> BenchmarkRun:
    """Run the requested offline workflow schedule once per arm."""

    cases = tuple(spec.get("cases") or CASE_IDS)
    variants = tuple(spec.get("variants") or WORKFLOW_VARIANT_NAMES)
    repeats = int(spec.get("repeats", 1))
    if repeats < 1 or repeats > MAX_REPEATS:
        raise ValueError("repeats_out_of_range")
    unknown_cases = [item for item in cases if item not in CASE_IDS]
    if unknown_cases:
        raise ValueError(f"unknown_case:{unknown_cases[0]}")
    for name in variants:
        workflow_variant(name)
    fingerprint = _condition_fingerprint(
        {
            "suite": "workflow",
            "cases": list(cases),
            "variants": list(variants),
            "repeats": repeats,
        }
    )
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="offline_scripted",
        suite="workflow",
        started_at=_now(),
        revision=_repo_revision(),
        spec={
            "cases": list(cases),
            "variants": list(variants),
            "repeats": repeats,
            "budgets": {"repeats_max": MAX_REPEATS},
        },
        condition_fingerprint=fingerprint,
    )
    for case_id in cases:
        for variant in variants:
            for repetition in range(1, repeats + 1):
                run.rows.append(
                    run_offline_arm(case_id, variant, repetition=repetition)
                )
    run.rows.sort(key=lambda item: (item.case_id, item.variant, item.repetition))
    positive_cases = {
        case_id
        for case_id in cases
        if str(load_manifest(case_id).expected_behavior.get("outcome", ""))
        in {"success", "success_after_repair"}
    }
    positive_rows = [row for row in run.rows if row.case_id in positive_cases]
    evaluated = bool(positive_rows) and all(
        row.status not in {"error", "not_started"}
        and row.total_tokens is not None
        and row.coverage.get("total_tokens", False)
        for row in positive_rows
    )
    verified_tasks = sum(
        row.verified_tasks
        for row in positive_rows
        if row.correct is True
    )
    numerator = sum(
        row.total_tokens or 0 for row in positive_rows
    )
    run.metrics = {
        "positive_arms": len(positive_rows),
        "verified_tasks": verified_tasks,
        "incurred_positive_tokens": numerator if evaluated else None,
        "tokens_per_verified_task": (
            round(numerator / verified_tasks, 2)
            if evaluated and verified_tasks > 0
            else None
        ),
        "amortized_evaluable": bool(evaluated and verified_tasks > 0),
    }
    run.completed_at = _now()
    run.incomplete = any(row.status in {"error", "not_started"} for row in run.rows)
    log.info(
        "[Benchmark.matrix] rows=%s incomplete=%s fingerprint=%s",
        len(run.rows),
        run.incomplete,
        fingerprint[:19],
    )
    return run


def run_live_matrix(spec: Mapping[str, Any]) -> BenchmarkRun:
    """Execute explicitly admitted positive arms sequentially.

    The caller must have run the strict admission preflight first. A missing
    usage field, timeout or infrastructure failure stops further billable work.
    """

    cases = tuple(spec.get("cases") or ())
    repeats = int(spec.get("repeats", 1))
    config_paths = tuple(Path(item) for item in spec.get("config_paths") or ())
    deadline_s = float(spec.get("deadline_s") or 0)
    started = time.monotonic()
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="deepseek_live",
        suite="workflow",
        started_at=_now(),
        revision=_repo_revision(),
        spec={
            "cases": list(cases),
            "repeats": repeats,
            "limits": {
                "max_tokens": spec.get("max_tokens"),
                "max_cost_usd": spec.get("max_cost_usd"),
                "deadline_s": deadline_s,
            },
        },
        condition_fingerprint=_condition_fingerprint(
            {"suite": "workflow", "cases": list(cases), "repeats": repeats, "mode": "live"}
        ),
    )
    config_by_case = {
        path.stem: path for path in config_paths if path.suffix in {".yaml", ".yml"}
    }
    stop_reason: str | None = None
    for case_id in cases:
        for repetition in range(1, repeats + 1):
            arm_id = f"{case_id}:v2-live:r{repetition}"
            row = ArmRow(
                arm_id=arm_id,
                suite="workflow",
                case_id=case_id,
                variant="v2-live",
                repetition=repetition,
                expected_outcome="success",
                provenance="deepseek_live",
                status="not_started",
            )
            if stop_reason is not None:
                row.reason = stop_reason
                run.rows.append(row)
                continue
            if time.monotonic() - started > deadline_s:
                stop_reason = "deadline_exceeded"
                row.reason = stop_reason
                run.rows.append(row)
                continue
            scratch = Path(tempfile.mkdtemp(prefix="lw-live-"))
            try:
                origin = materialize(case_id, scratch / "origin").root
                manifest = load_manifest(case_id)
                config_path = config_by_case.get(case_id)
                if config_path is None:
                    raise RuntimeError("live_config_missing")
                session = FeatureSession(source_cwd=origin)
                view = session.start(
                    manifest.request,
                    mode="auto",
                    config_path=config_path,
                )
                row.feature_id = view.feature_id
                row.status = view.status
                row.observed_outcome = view.status
                row.reason = view.reason
                worktree = worktree_for(origin, view.feature_id)
                snapshot = _journal(worktree).reporting_snapshot(view.feature_id)
                report = aggregate_usage(snapshot)
                totals = report.totals
                if totals is not None:
                    row.input_tokens = totals.input_tokens
                    row.output_tokens = totals.output_tokens
                    row.total_tokens = totals.total_tokens
                    row.cache_hit_tokens = totals.cache_hit_tokens
                    row.reasoning_tokens = totals.reasoning_tokens
                    row.logical_calls = totals.logical_calls
                    row.retry_attempts = totals.retry_attempts
                    row.coverage = totals.coverage.complete()
                oracle = all(
                    item.ok for item in run_all_oracles(manifest, worktree)
                )
                row.oracle_ok = oracle
                row.correct = view.status == "success" and oracle
                if not row.coverage.get("total_tokens", False):
                    stop_reason = "incomplete_usage"
                if not oracle:
                    stop_reason = "incorrect_outcome"
            except Exception as exc:  # noqa: BLE001 - stop on unknown billable work
                row.status = "error"
                row.correct = False
                row.error_class = type(exc).__name__
                stop_reason = "arm_error"
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            run.rows.append(row)
            if stop_reason is not None:
                log.warning(
                    "[Benchmark.live] stop=%s arm=%s", stop_reason, arm_id
                )
    run.completed_at = _now()
    run.incomplete = bool(stop_reason) or any(
        row.status == "not_started" for row in run.rows
    )
    return run


def run_retrieval_matrix(spec: Mapping[str, Any]) -> BenchmarkRun:
    from lean_weave.benchmark.retrieval import VARIANT_NAMES, load_cases

    case_ids = tuple(spec.get("cases") or [item.id for item in load_cases()])
    variants = tuple(spec.get("variants") or VARIANT_NAMES)
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="local_estimate",
        suite="retrieval",
        started_at=_now(),
        revision=_repo_revision(),
        spec={"cases": list(case_ids), "variants": list(variants)},
        condition_fingerprint=_condition_fingerprint(
            {"suite": "retrieval", "cases": list(case_ids), "variants": list(variants)}
        ),
    )
    scratch = Path(tempfile.mkdtemp(prefix="lw-retrieval-matrix-"))
    try:
        for case_id in case_ids:
            for variant in variants:
                started = time.monotonic()
                row = ArmRow(
                    arm_id=f"{case_id}:{variant}",
                    suite="retrieval",
                    case_id=case_id,
                    variant=variant,
                    repetition=1,
                    expected_outcome="local_estimate",
                    provenance="local_estimate",
                    status="complete",
                )
                try:
                    payload = evaluate_retrieval(case_id, variant, scratch)
                    row.observed_outcome = payload["pack"]["status"]
                    row.correct = None
                    row.retrieval = payload
                    row.fixture_fingerprint = payload.get("fixture_fingerprint")
                    row.conditions = payload.get("conditions", {})
                    row.duration_ms = payload.get("timing_ms", {}).get("total")
                except Exception as exc:  # noqa: BLE001 - keep every scheduled row
                    row.status = "error"
                    row.error_class = type(exc).__name__
                    row.correct = False
                row.duration_ms = row.duration_ms or int(
                    (time.monotonic() - started) * 1000
                )
                run.rows.append(row)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    run.rows.sort(key=lambda item: (item.case_id, item.variant, item.repetition))
    run.completed_at = _now()
    run.incomplete = any(row.status == "error" for row in run.rows)
    return run
