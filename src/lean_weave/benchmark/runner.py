"""Matched offline benchmark runner over the public FeatureSession."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2

from lean_weave.benchmark.conditions import (
    canonical_digest,
    capture_arm_conditions,
    release_binding,
    scenario_definition,
    source_revision,
    source_tree_digest,
)
from lean_weave.benchmark.fixtures import (
    CASE_IDS,
    intended_edit_files,
    load_manifest,
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
from lean_weave.benchmark.results import (
    aggregate_component_evidence,
    project_attempt_evidence,
    project_request_components,
)
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.analytics import aggregate_usage
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.workflow.session import FeatureSession
from lean_weave.workflow.state import JsonCheckpointStore

log = get_logger("benchmark.runner")

MAX_REPEATS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _negative_matches(
    case_id: str,
    expected: str,
    status: str,
    reason: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    changed_paths: Sequence[str] | None = None,
) -> bool:
    evidence = evidence or {}
    dispatch = evidence.get("dispatch")
    if expected == "budget_exhausted":
        return (
            status == "budget_exhausted"
            and "feature_token_limit" in (reason or "")
            and dispatch == "no_send"
        )
    if expected == "failed":
        if case_id == "insufficient_context":
            return (
                status == "failed"
                and "invalid_plan" in (reason or "")
                and not changed_paths
                and evidence.get("verification_sends", 0) == 0
                and evidence.get("checks_observed", 0) == 0
            )
        return status == "failed" and not changed_paths
    return False


def _changed_paths(workspace: Path) -> list[str]:
    """Tracked/untracked paths changed inside a managed worktree."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-uall"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        candidate = line[3:].strip()
        if not candidate or candidate.startswith(".lean-weave"):
            continue
        paths.append(candidate)
    return sorted(paths)


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
        row.origin_head = manifest.expected_head
        row.conditions = capture_arm_conditions(
            manifest=manifest,
            origin_head=manifest.expected_head,
            variant_name=variant.name,
            context_mode=variant.context_mode,
            context_extra=variant.context_extra,
            config_path=case.config_path,
        )
        row.fixture_fingerprint = canonical_digest(
            {
                "head": row.conditions["fixture"]["pinned_head"],
                "manifest": row.conditions["fixture"]["manifest_digest"],
            }
        )
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
        workflow_ms = int((time.monotonic() - started) * 1000)
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
        row.evidence = project_attempt_evidence(
            snapshot=snapshot,
            control=envelope.control,
            transport_sends=transport.send_count,
        )
        changed = _changed_paths(worktree)
        row.evidence["workspace_changes"] = changed
        oracle_ms = 0
        if row.expected_negative:
            row.correct = _negative_matches(
                case_id,
                expected,
                view.status,
                view.reason,
                evidence=row.evidence,
                changed_paths=_changed_paths(worktree),
            )
            row.oracle_ok = None
        else:
            oracle_started = time.monotonic()
            oracle = all(item.ok for item in run_all_oracles(manifest, worktree))
            oracle_ms = int((time.monotonic() - oracle_started) * 1000)
            row.oracle_ok = oracle
            minimum_retries = int(manifest.expected_behavior.get("min_retry_count", 0) or 0)
            row.correct = bool(
                view.status == "success"
                and oracle
                and (row.workflow_retries or 0) >= minimum_retries
            )
        row.evidence["input_components"] = project_request_components(
            requests=transport.requests,
            attempts=snapshot.attempts,
        )
        row.duration_ms = int((time.monotonic() - started) * 1000)
        row.evidence["workflow_wall_ms"] = workflow_ms
        row.evidence["oracle_ms"] = oracle_ms
        row.evidence["collection_ms"] = max(
            0, row.duration_ms - workflow_ms - oracle_ms
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

    raw_cases = list(spec.get("cases") or CASE_IDS)
    raw_variants = list(spec.get("variants") or WORKFLOW_VARIANT_NAMES)
    if len(set(raw_cases)) != len(raw_cases):
        raise ValueError("duplicate_case_selector")
    if len(set(raw_variants)) != len(raw_variants):
        raise ValueError("duplicate_variant_selector")
    cases = tuple(raw_cases)
    variants = tuple(raw_variants)
    repeats = int(spec.get("repeats", 1))
    if repeats < 1 or repeats > MAX_REPEATS:
        raise ValueError("repeats_out_of_range")
    unknown_cases = [item for item in cases if item not in CASE_IDS]
    if unknown_cases:
        raise ValueError(f"unknown_case:{unknown_cases[0]}")
    for name in variants:
        workflow_variant(name)
    revision = source_revision()
    tree_digest = source_tree_digest()
    binding = release_binding()
    schedule: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    manifests = {case_id: load_manifest(case_id) for case_id in cases}
    for repetition in range(1, repeats + 1):
        order = variants if repetition % 2 else tuple(reversed(variants))
        for case_id in cases:
            for variant_name in order:
                key = (case_id, variant_name, repetition)
                if key in seen:
                    raise ValueError("duplicate_schedule_entry")
                seen.add(key)
                variant = workflow_variant(variant_name)
                schedule.append(
                    scenario_definition(
                        case_id=case_id,
                        variant=variant.name,
                        repetition=repetition,
                        manifest=manifests[case_id],
                        context_mode=variant.context_mode,
                        context_extra=variant.context_extra,
                    )
                )
    fingerprint = canonical_digest(
        {
            "suite": "workflow",
            "schedule": schedule,
            "fixtures": {
                case_id: manifest.expected_head
                for case_id, manifest in manifests.items()
            },
            "source_revision": revision,
            "source_tree_digest": tree_digest,
            "release_binding": binding,
        }
    )
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="offline_scripted",
        suite="workflow",
        started_at=_now(),
        revision=revision,
        source_revision=revision,
        spec={
            "cases": list(cases),
            "variants": list(variants),
            "repeats": repeats,
            "schedule": schedule,
            "budgets": {"repeats_max": MAX_REPEATS},
            "source_tree_digest": tree_digest,
            "release_binding": binding,
        },
        condition_fingerprint=fingerprint,
    )
    for entry in schedule:
        run.rows.append(
            run_offline_arm(
                entry["case_id"], entry["variant"], repetition=entry["repetition"]
            )
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
        "input_components": aggregate_component_evidence(run.rows),
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
    """Execute explicitly admitted live arms sequentially.

    The caller must have run the strict admission preflight first. A missing
    usage field, timeout or infrastructure failure stops further billable work.
    """

    from lean_weave.benchmark.live import run_live_case

    cases = tuple(spec.get("cases") or ())
    repeats = int(spec.get("repeats", 1))
    deadline_s = float(spec.get("deadline_s") or 0)
    arms = list(spec.get("arms") or ())
    config_paths = tuple(Path(item) for item in spec.get("config_paths") or ())
    started = time.monotonic()
    deadline_at = started + max(0.001, deadline_s)
    variants = tuple(spec.get("variants") or ())
    if arms:
        variants = tuple(dict.fromkeys(str(arm.get("variant")) for arm in arms))
    schedule: list[dict[str, Any]] = []
    for repetition in range(1, repeats + 1):
        order = variants if repetition % 2 else tuple(reversed(variants))
        for case_id in cases:
            for variant in order:
                arm = next(
                    (
                        item
                        for item in arms
                        if item.get("case_id") == case_id
                        and item.get("variant") == variant
                    ),
                    None,
                )
                if arm is None:
                    continue
                schedule.append(
                    {
                        "suite": "workflow",
                        "case_id": case_id,
                        "variant": variant,
                        "repetition": repetition,
                        "context_mode": arm.get("context_mode"),
                        "config_path": arm.get("config_path"),
                    }
                )
    if not schedule:
        # Legacy single-arm live invocation: one arm per case.
        for case_id in cases:
            for repetition in range(1, repeats + 1):
                schedule.append(
                    {
                        "suite": "workflow",
                        "case_id": case_id,
                        "variant": "v1-live",
                        "repetition": repetition,
                        "context_mode": "v1",
                        "config_path": None,
                    }
                )
    revision = source_revision()
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="deepseek_live",
        suite="workflow",
        started_at=_now(),
        revision=revision,
        source_revision=revision,
        spec={
            "cases": list(cases),
            "variants": list(variants),
            "repeats": repeats,
            "schedule": schedule,
            "source_tree_digest": source_tree_digest(),
            "release_binding": release_binding(),
            "limits": {
                "max_tokens": spec.get("max_tokens"),
                "max_cost_usd": spec.get("max_cost_usd"),
                "deadline_s": deadline_s,
            },
        },
        condition_fingerprint=canonical_digest(
            {"suite": "workflow", "schedule": schedule, "mode": "live"}
        ),
    )
    config_by_case = {
        path.stem.split(".", 1)[0]: path
        for path in config_paths
        if path.suffix in {".yaml", ".yml"}
    }
    stop_reason: str | None = None
    for entry in schedule:
        case_id = entry["case_id"]
        variant = entry["variant"]
        repetition = entry["repetition"]
        arm_id = f"{case_id}:{variant}:r{repetition}"
        row = ArmRow(
            arm_id=arm_id,
            suite="workflow",
            case_id=case_id,
            variant=variant,
            repetition=repetition,
            expected_outcome="success",
            provenance="deepseek_live",
            status="not_started",
            conditions={"context_mode": entry.get("context_mode")},
        )
        if stop_reason is not None:
            row.reason = stop_reason
            run.rows.append(row)
            continue
        if time.monotonic() >= deadline_at:
            stop_reason = "deadline_exceeded"
            row.reason = stop_reason
            run.rows.append(row)
            continue
        scratch = Path(tempfile.mkdtemp(prefix="lw-live-"))
        try:
            config_path = (
                Path(entry["config_path"])
                if entry.get("config_path")
                else config_by_case.get(case_id)
            )
            if config_path is None:
                raise RuntimeError("live_config_missing")
            outcome = run_live_case(
                case_id,
                scratch,
                config_path=config_path,
                output_dir=scratch,
                deadline_s=deadline_s,
                deadline_at=deadline_at,
                variant=variant,
                context_mode=entry.get("context_mode"),
            )
            row.feature_id = outcome.feature_id
            row.status = outcome.status
            row.observed_outcome = outcome.status
            row.reason = outcome.reason
            row.oracle_ok = outcome.oracle_ok
            row.input_tokens = outcome.input_tokens
            row.output_tokens = outcome.output_tokens
            row.total_tokens = outcome.total_tokens
            row.coverage = outcome.coverage
            row.evidence = outcome.evidence
            row.logical_calls = outcome.logical_calls
            row.retry_attempts = outcome.retry_attempts
            row.duration_ms = outcome.duration_ms
            row.verification_status = outcome.verification_status
            row.correct = bool(
                outcome.status == "success"
                and outcome.oracle_ok is True
                and outcome.coverage.get("total_tokens", False)
                and outcome.evidence.get("dispatch") != "unknown"
            )
            if outcome.error == "live_timeout":
                stop_reason = "deadline_exceeded"
            elif outcome.evidence.get("dispatch") == "unknown":
                stop_reason = "unknown_dispatch"
            elif not outcome.coverage.get("total_tokens", False):
                stop_reason = "incomplete_usage"
            elif outcome.oracle_ok is not True:
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
            log.warning("[Benchmark.live] stop=%s arm=%s", stop_reason, arm_id)
    run.completed_at = _now()
    run.incomplete = bool(stop_reason) or any(
        row.status == "not_started" for row in run.rows
    )
    return run


def run_retrieval_matrix(spec: Mapping[str, Any]) -> BenchmarkRun:
    from lean_weave.benchmark.retrieval import VARIANT_NAMES, load_cases

    raw_cases = list(spec.get("cases") or [item.id for item in load_cases()])
    raw_variants = list(spec.get("variants") or VARIANT_NAMES)
    if len(set(raw_cases)) != len(raw_cases):
        raise ValueError("duplicate_case_selector")
    if len(set(raw_variants)) != len(raw_variants):
        raise ValueError("duplicate_variant_selector")
    known = {item.id for item in load_cases()}
    unknown = [item for item in raw_cases if item not in known]
    if unknown:
        raise ValueError(f"unknown_case:{unknown[0]}")
    for name in raw_variants:
        if name not in VARIANT_NAMES:
            raise ValueError(f"unknown_retrieval_variant:{name}")
    repeats = int(spec.get("repeats", 1))
    if repeats < 1 or repeats > MAX_REPEATS:
        raise ValueError("repeats_out_of_range")
    revision = source_revision()
    tree_digest = source_tree_digest()
    binding = release_binding()
    schedule: list[dict[str, Any]] = []
    for repetition in range(1, repeats + 1):
        order = raw_variants if repetition % 2 else list(reversed(raw_variants))
        for case_id in raw_cases:
            for variant in order:
                schedule.append(
                    {
                        "suite": "retrieval",
                        "case_id": case_id,
                        "variant": variant,
                        "repetition": repetition,
                        "scenario_digest": canonical_digest(
                            {
                                "suite": "retrieval",
                                "case_id": case_id,
                                "variant": variant,
                                "repetition": repetition,
                            }
                        ),
                    }
                )
    run = BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        evidence_source="local_estimate",
        suite="retrieval",
        started_at=_now(),
        revision=revision,
        source_revision=revision,
        spec={
            "cases": raw_cases,
            "variants": raw_variants,
            "repeats": repeats,
            "schedule": schedule,
            "budgets": {"repeats_max": MAX_REPEATS},
            "source_tree_digest": tree_digest,
            "release_binding": binding,
        },
        condition_fingerprint=canonical_digest(
            {
                "suite": "retrieval",
                "schedule": schedule,
                "source_revision": revision,
                "source_tree_digest": tree_digest,
                "release_binding": binding,
            }
        ),
    )
    for entry in schedule:
        case_id = entry["case_id"]
        variant = entry["variant"]
        repetition = entry["repetition"]
        started = time.monotonic()
        row = ArmRow(
            arm_id=f"{case_id}:{variant}:r{repetition}",
            suite="retrieval",
            case_id=case_id,
            variant=variant,
            repetition=repetition,
            expected_outcome="local_estimate",
            provenance="local_estimate",
            status="complete",
        )
        arm_scratch = Path(tempfile.mkdtemp(prefix="lw-retrieval-arm-"))
        try:
            payload = evaluate_retrieval(case_id, variant, arm_scratch)
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
        finally:
            shutil.rmtree(arm_scratch, ignore_errors=True)
        row.duration_ms = row.duration_ms or int(
            (time.monotonic() - started) * 1000
        )
        run.rows.append(row)
    run.rows.sort(key=lambda item: (item.case_id, item.variant, item.repetition))
    run.completed_at = _now()
    run.incomplete = any(row.status == "error" for row in run.rows)
    return run
