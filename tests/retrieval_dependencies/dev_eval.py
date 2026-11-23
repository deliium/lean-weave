"""Standalone development-only scorecard for V1 implement vs V2 packer baselines.

Not registered with the historical benchmark suite. Labels stay evaluator-only.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from lean_weave.app import AppContext, create_code_index, create_context_packer
from lean_weave.code_index import CodeIndex
from lean_weave.config.models import AppConfig, ContextConfig, DependencyCoverageConfig
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import ExpansionRequest, PackRequest
from lean_weave.context.packing_tokens import ConservativeByteCounter, render_fragment_header
from lean_weave.context.provider import DeterministicContextProvider
from lean_weave.errors import ContextBudgetError
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage

from tests.retrieval_dependencies.dev_cases import (
    BASELINE_REVISION,
    COMMON_MODEL,
    COMMON_OUTPUT_RESERVE,
    DEV_CASES,
    DevCase,
    EvidenceInterval,
    PROFILES,
    case_file_digests,
    fixture_hash,
    fixture_manifest,
    v1_context_config,
    v2_packing_config,
)

# Candidate arm uses the production default dependency-coverage bounds.
CANDIDATE_COVERAGE = DependencyCoverageConfig(
    enabled=True,
    max_depth=2,
    max_definitions=8,
    max_tokens=1024,
)

log = get_logger("tests.retrieval_dependencies.dev_eval")

ArmName = Literal["v1", "v2", "candidate"]
KNOWN_ARMS = ("v1", "v2", "candidate")
REQUIRED_NON_TIMING = (
    "case_id",
    "arm",
    "classification",
    "ready",
    "status",
    "reason_codes",
    "delivered_intervals",
    "anchor_covered",
    "must_cover_hits",
    "must_cover_misses",
    "missing_dependency_count",
    "forbidden_emissions",
    "wire_input_tokens",
    "wire_total_tokens",
    "native_input_tokens",
    "native_output_tokens",
    "native_total_tokens",
    "dependency_attributed_tokens",
    "expansion_attempts",
    "expansion_successes",
    "expansion_applicable",
    "scope_paths",
    "profile",
)


@dataclass(frozen=True)
class DeliveredUnit:
    path: str
    start_line: int
    end_line: int
    digest: str
    kind: str
    text: str
    unit_id: str


@dataclass
class ArmRun:
    case_id: str
    arm: ArmName
    classification: str
    profile: str
    ready: bool
    status: str
    reason_codes: tuple[str, ...]
    delivered_intervals: tuple[dict[str, Any], ...]
    anchor_covered: int
    must_cover_hits: int
    must_cover_misses: int
    missing_dependency_count: int
    forbidden_emissions: int
    wire_input_tokens: int | None
    wire_total_tokens: int | None
    native_input_tokens: int | None
    native_output_tokens: int | None
    native_total_tokens: int | None
    dependency_attributed_tokens: int | None
    expansion_attempts: int
    expansion_successes: int
    expansion_applicable: bool | None
    scope_paths: tuple[str, ...]
    latency_ms: float | None = None
    notes: tuple[str, ...] = ()


@dataclass
class ScoreSummary:
    fixture_hash: str
    baseline_revision: str
    arms: tuple[str, ...]
    case_runs: list[ArmRun] = field(default_factory=list)
    aggregates: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def interval_fully_covered(
    expected: EvidenceInterval,
    units: Sequence[DeliveredUnit],
    *,
    file_digests: dict[str, str],
) -> bool:
    expected_digest = file_digests.get(expected.path)
    if expected_digest is None:
        return False
    for unit in units:
        if unit.path != expected.path:
            continue
        if unit.digest != expected_digest:
            continue
        if unit.start_line <= expected.start_line and unit.end_line >= expected.end_line:
            return True
    return False


def intervals_adjacent_or_gapped(
    left: tuple[int, int],
    right: tuple[int, int],
) -> Literal["adjacent", "overlapping", "gapped"]:
    a_start, a_end = left
    b_start, b_end = right
    if a_end + 1 == b_start or b_end + 1 == a_start:
        return "adjacent"
    if a_start <= b_end and b_start <= a_end:
        return "overlapping"
    return "gapped"


def charge_dependency_tokens(
    units: Sequence[DeliveredUnit],
    labels: Sequence[EvidenceInterval],
    *,
    file_digests: dict[str, str],
    counter: ConservativeByteCounter,
) -> int:
    """Offline label attribution: charge each intersecting emitted unit once."""

    charged: set[str] = set()
    total = 0
    for label in labels:
        expected_digest = file_digests.get(label.path)
        if expected_digest is None:
            continue
        for unit in units:
            if unit.unit_id in charged:
                continue
            if unit.path != label.path or unit.digest != expected_digest:
                continue
            if unit.end_line < label.start_line or unit.start_line > label.end_line:
                continue
            header = render_fragment_header(
                unit.path, unit.start_line, unit.end_line, unit.digest, unit.kind
            )
            total += counter.count_text(header + "\n" + unit.text)
            charged.add(unit.unit_id)
    return total


def wire_token_counts(
    messages: Sequence[dict[str, Any]] | None,
    *,
    counter: ConservativeByteCounter,
    output_tokens: int,
) -> tuple[int | None, int | None]:
    if messages is None:
        return None, None
    payload = {
        "model": COMMON_MODEL,
        "messages": list(messages),
        "tools": [],
    }
    input_tokens = counter.count_request(payload)
    return input_tokens, input_tokens + output_tokens


def _unit_id(path: str, start: int, end: int, digest: str) -> str:
    return f"{path}:{start}-{end}:{digest}"


def _v1_units(excerpts: Iterable[Any]) -> list[DeliveredUnit]:
    units: list[DeliveredUnit] = []
    for item in excerpts:
        units.append(
            DeliveredUnit(
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                digest=item.digest,
                kind=item.kind,
                text=item.text,
                unit_id=_unit_id(item.path, item.start_line, item.end_line, item.digest),
            )
        )
    return units


def _v2_units(fragments: Iterable[Any]) -> list[DeliveredUnit]:
    units: list[DeliveredUnit] = []
    for item in fragments:
        units.append(
            DeliveredUnit(
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                digest=item.digest,
                kind=item.kind,
                text=item.text,
                unit_id=_unit_id(item.path, item.start_line, item.end_line, item.digest),
            )
        )
    return units


def _score_units(
    case: DevCase,
    units: Sequence[DeliveredUnit],
    *,
    ready: bool,
) -> dict[str, Any]:
    digests = case_file_digests(case)
    if not ready:
        return {
            "anchor_covered": 0,
            "must_cover_hits": 0,
            "must_cover_misses": len(case.must_cover),
            "missing_dependency_count": len(case.must_cover),
            "forbidden_emissions": 0,
            "delivered_intervals": (),
        }
    anchors = sum(
        1 for item in case.anchors if interval_fully_covered(item, units, file_digests=digests)
    )
    hits = sum(
        1
        for item in case.must_cover
        if interval_fully_covered(item, units, file_digests=digests)
    )
    misses = len(case.must_cover) - hits
    forbidden = sum(
        1
        for item in case.negatives
        if interval_fully_covered(item, units, file_digests=digests)
    )
    delivered = tuple(
        {
            "path": unit.path,
            "start_line": unit.start_line,
            "end_line": unit.end_line,
            "digest": unit.digest,
            "kind": unit.kind,
        }
        for unit in units
    )
    return {
        "anchor_covered": anchors,
        "must_cover_hits": hits,
        "must_cover_misses": misses,
        "missing_dependency_count": misses,
        "forbidden_emissions": forbidden,
        "delivered_intervals": delivered,
    }


def _feature_state(case: DevCase) -> FeatureState:
    return FeatureState(
        feature_id=f"dev-{case.case_id}",
        stage=Stage.IMPLEMENT,
        request=case.task_query,
        plan=FeaturePlan(
            goal=case.task_query,
            settings={"testing": False, "logging": "verbose", "docs": False},
            tasks=[
                ImplementationTask(
                    id="t1",
                    title=case.task_title,
                    description=case.task_description,
                    files=[case.edit_target],
                )
            ],
        ),
        active_task_id="t1",
    )


def _pack_request(case: DevCase) -> PackRequest:
    return PackRequest(
        task_id="t1",
        query=case.task_query,
        task_specification=case.task_description,
        system_instructions="Use only supplied source. Prefer exact definitions.",
        level="l1",
        required_paths=(case.edit_target,),
    )


def run_v1_arm(case: DevCase, root: Path) -> ArmRun:
    profile = PROFILES[case.profile]
    config = v1_context_config(profile)
    provider = DeterministicContextProvider(root, context_config=config)
    state = _feature_state(case)
    counter = ConservativeByteCounter()
    started = time.perf_counter()
    try:
        bundle = provider.build("implement", state)
        latency_ms = (time.perf_counter() - started) * 1000.0
    except ContextBudgetError:
        latency_ms = (time.perf_counter() - started) * 1000.0
        scored = _score_units(case, (), ready=False)
        log.debug(
            "[DevEval] arm=v1 case=%s ready=false reason=required_source_over_budget",
            case.case_id,
        )
        return ArmRun(
            case_id=case.case_id,
            arm="v1",
            classification=case.classification,
            profile=case.profile,
            ready=False,
            status="insufficient_context",
            reason_codes=("required_source_over_budget",),
            delivered_intervals=(),
            anchor_covered=0,
            must_cover_hits=0,
            must_cover_misses=scored["must_cover_misses"],
            missing_dependency_count=scored["missing_dependency_count"],
            forbidden_emissions=0,
            wire_input_tokens=0,
            wire_total_tokens=0,
            native_input_tokens=0,
            native_output_tokens=None,
            native_total_tokens=0,
            dependency_attributed_tokens=0,
            expansion_attempts=0,
            expansion_successes=0,
            expansion_applicable=None,
            scope_paths=(case.edit_target,),
            latency_ms=latency_ms,
            notes=("v1_expansion_not_applicable", "unready_zero_delivered"),
        )
    pack = bundle["pack"]
    messages = bundle["messages"]
    units = _v1_units(pack.excerpts)
    scored = _score_units(case, units, ready=True)
    wire_in, wire_total = wire_token_counts(
        messages, counter=counter, output_tokens=COMMON_OUTPUT_RESERVE
    )
    dep_tokens = charge_dependency_tokens(
        units,
        case.must_cover,
        file_digests=case_file_digests(case),
        counter=counter,
    )
    scope = tuple(sorted(pack.relevant_paths))
    log.debug(
        "[DevEval] arm=v1 case=%s ready=true excerpts=%s missing=%s",
        case.case_id,
        len(units),
        scored["missing_dependency_count"],
    )
    return ArmRun(
        case_id=case.case_id,
        arm="v1",
        classification=case.classification,
        profile=case.profile,
        ready=True,
        status="ready",
        reason_codes=(),
        delivered_intervals=scored["delivered_intervals"],
        anchor_covered=scored["anchor_covered"],
        must_cover_hits=scored["must_cover_hits"],
        must_cover_misses=scored["must_cover_misses"],
        missing_dependency_count=scored["missing_dependency_count"],
        forbidden_emissions=scored["forbidden_emissions"],
        wire_input_tokens=wire_in,
        wire_total_tokens=wire_total,
        native_input_tokens=int(pack.estimated_tokens),
        native_output_tokens=None,
        native_total_tokens=int(pack.estimated_tokens),
        dependency_attributed_tokens=dep_tokens,
        expansion_attempts=0,
        expansion_successes=0,
        expansion_applicable=None,
        scope_paths=scope,
        latency_ms=latency_ms,
        notes=("v1_expansion_not_applicable",),
    )


def _run_label_free_expansion(
    packer: ContextPacker,
    session,
    case: DevCase,
    *,
    units: list[DeliveredUnit],
) -> tuple[list[DeliveredUnit], int, int]:
    """Try first uncovered registered symbol candidate, sorted stably."""

    if not case.expansion.enabled or session is None:
        return units, 0, 0
    digests = case_file_digests(case)
    attempts = 0
    successes = 0
    remaining = case.expansion.max_attempts
    while remaining > 0:
        uncovered = [
            item
            for item in case.must_cover
            if item.role == "definition"
            and not interval_fully_covered(item, units, file_digests=digests)
        ]
        # Label-free policy uses registered candidates only; uncovered labels
        # gate stopping, never selector construction from expected intervals.
        candidates = [
            cand
            for cand in session.candidates
            if cand.kind == "symbol"
            and cand.qualified_name
            and cand.start_line is not None
            and cand.end_line is not None
        ]
        candidates.sort(
            key=lambda item: (
                item.path,
                item.qualified_name or "",
                item.start_line or 0,
                item.end_line or 0,
            )
        )
        chosen = None
        for cand in candidates:
            already = any(
                unit.path == cand.path
                and unit.start_line == cand.start_line
                and unit.end_line == cand.end_line
                and unit.digest == cand.digest
                for unit in units
            )
            if already:
                continue
            chosen = cand
            break
        if chosen is None:
            break
        digest = session.revisions.get(chosen.path) or chosen.digest
        request = ExpansionRequest(
            kind="symbol",
            path=chosen.path,
            expected_digest=digest,
            qualified_name=chosen.qualified_name,
        )
        attempts += 1
        remaining -= 1
        result = packer.expand(session, request)
        if result.status != "ready" or result.pack is None:
            log.debug(
                "[DevEval] expansion_failed case=%s status=%s reasons=%s",
                case.case_id,
                result.status,
                result.reason_codes,
            )
            break
        successes += 1
        units = _v2_units(result.pack.fragments)
        if not uncovered:
            # Still allow one successful delivery for control expansion cases.
            pass
    return units, attempts, successes


def run_v2_arm(
    case: DevCase,
    root: Path,
    cache_base: Path,
    *,
    arm: ArmName = "v2",
) -> ArmRun:
    if arm not in {"v2", "candidate"}:
        raise ValueError(f"unsupported v2-family arm: {arm}")
    profile = PROFILES[case.profile]
    packing = v2_packing_config(profile)
    coverage = (
        CANDIDATE_COVERAGE if arm == "candidate" else DependencyCoverageConfig()
    )
    app = AppContext(
        config=AppConfig(
            context=ContextConfig(packing=packing, dependency_coverage=coverage)
        ),
        config_source=None,
    )
    index = create_code_index(app, workspace_root=root, cache_base=cache_base)
    if not isinstance(index, CodeIndex):
        raise RuntimeError("CodeIndex construction failed")
    index.build()
    packer = create_context_packer(app, workspace_root=root, cache_base=cache_base)
    request = _pack_request(case)
    counter = ConservativeByteCounter()
    started = time.perf_counter()
    result, session = packer.open_task(request)
    latency_ms = (time.perf_counter() - started) * 1000.0
    ready = result.status == "ready" and result.pack is not None and bool(result.pack.messages)
    units: list[DeliveredUnit] = []
    messages = None
    native_in = None
    native_out = None
    native_total = None
    expansion_attempts = 0
    expansion_successes = 0
    if ready and result.pack is not None:
        units = _v2_units(result.pack.fragments)
        messages = result.pack.messages
        native_in = int(result.pack.metrics.input_tokens)
        native_out = int(result.pack.metrics.output_tokens)
        native_total = int(result.pack.metrics.total_tokens)
        if case.expansion.enabled:
            units, expansion_attempts, expansion_successes = _run_label_free_expansion(
                packer, session, case, units=units
            )
            if result.pack is not None and expansion_successes:
                # Re-count wire from the latest delivered pack when expansion succeeded.
                # expand() returns a new pack; recover messages from the last expand via units only
                # for coverage. Wire tokens stay from the initial ready pack unless we re-open.
                pass
    scored = _score_units(case, units, ready=ready)
    if not ready:
        wire_in, wire_total = None, None
        dep_tokens = None
    else:
        wire_in, wire_total = wire_token_counts(
            messages, counter=counter, output_tokens=packing.output_tokens
        )
        dep_tokens = charge_dependency_tokens(
            units,
            case.must_cover,
            file_digests=case_file_digests(case),
            counter=counter,
        )
    scope = (case.edit_target,)
    log.debug(
        "[DevEval] arm=%s case=%s ready=%s status=%s missing=%s",
        arm,
        case.case_id,
        ready,
        result.status,
        scored["missing_dependency_count"],
    )
    return ArmRun(
        case_id=case.case_id,
        arm=arm,
        classification=case.classification,
        profile=case.profile,
        ready=ready,
        status=result.status,
        reason_codes=tuple(result.reason_codes),
        delivered_intervals=scored["delivered_intervals"],
        anchor_covered=scored["anchor_covered"],
        must_cover_hits=scored["must_cover_hits"],
        must_cover_misses=scored["must_cover_misses"],
        missing_dependency_count=scored["missing_dependency_count"],
        forbidden_emissions=scored["forbidden_emissions"],
        wire_input_tokens=wire_in if ready else 0,
        wire_total_tokens=wire_total if ready else 0,
        native_input_tokens=native_in if ready else 0,
        native_output_tokens=native_out if ready else 0,
        native_total_tokens=native_total if ready else 0,
        dependency_attributed_tokens=dep_tokens if ready else 0,
        expansion_attempts=expansion_attempts,
        expansion_successes=expansion_successes,
        expansion_applicable=True,
        scope_paths=scope,
        latency_ms=latency_ms,
        notes=() if ready else ("unready_zero_delivered",),
    )


def run_case_arm(
    case: DevCase,
    arm: ArmName,
    *,
    workspace: Path,
    cache_base: Path,
) -> ArmRun:
    write_tree(workspace, case.files)
    if arm == "v1":
        return run_v1_arm(case, workspace)
    if arm in {"v2", "candidate"}:
        return run_v2_arm(case, workspace, cache_base, arm=arm)
    raise ValueError(f"unknown arm: {arm}")


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def measure_latency(
    case: DevCase,
    arm: ArmName,
    *,
    repeats: int,
    warm: bool,
) -> dict[str, Any]:
    samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="lw-dev-lat-") as tmp:
        base = Path(tmp)
        workspace = base / "repo"
        cache = base / "cache"
        cache.mkdir(mode=0o700)
        workspace.mkdir()
        write_tree(workspace, case.files)
        # one warm-up excluded from statistics
        run_case_arm(case, arm, workspace=workspace, cache_base=cache)
        for _ in range(repeats):
            if not warm:
                # cold: wipe cache between samples for v2-family arms
                if arm != "v1":
                    for child in cache.iterdir():
                        if child.is_file():
                            child.unlink()
                        else:
                            import shutil

                            shutil.rmtree(child)
                    cache.mkdir(mode=0o700, exist_ok=True)
            result = run_case_arm(case, arm, workspace=workspace, cache_base=cache)
            if result.latency_ms is None:
                raise RuntimeError("latency sample missing")
            samples.append(result.latency_ms)
    return {
        "case_id": case.case_id,
        "arm": arm,
        "warm": warm,
        "repeats": repeats,
        "median_ms": statistics.median(samples),
        "p95_ms": _percentile(samples, 95.0),
        "samples_ms": samples,
    }


def aggregate_runs(runs: Sequence[ArmRun]) -> dict[str, Any]:
    by_arm: dict[str, list[ArmRun]] = {}
    for run in runs:
        by_arm.setdefault(run.arm, []).append(run)
    summary: dict[str, Any] = {}
    for arm, items in sorted(by_arm.items()):
        coverage = [item for item in items if item.classification == "coverage"]
        eligible_expansion = [
            item for item in items if item.expansion_applicable and item.classification == "control"
        ]
        hit = sum(item.must_cover_hits for item in coverage)
        total = sum(item.must_cover_hits + item.must_cover_misses for item in coverage)
        ready_items = [item for item in items if item.ready]
        unready = [item for item in items if not item.ready]
        expansion_success_cases = sum(1 for item in eligible_expansion if item.expansion_successes)
        summary[arm] = {
            "cases": len(items),
            "coverage_cases": len(coverage),
            "recall_micro": (hit / total) if total else None,
            "missing_dependency_total": sum(item.missing_dependency_count for item in coverage),
            "forbidden_emissions_total": sum(item.forbidden_emissions for item in items),
            "wire_input_tokens_ready_sum": sum(
                item.wire_input_tokens or 0 for item in ready_items
            ),
            "wire_total_tokens_ready_sum": sum(
                item.wire_total_tokens or 0 for item in ready_items
            ),
            "dependency_attributed_tokens_ready_sum": sum(
                item.dependency_attributed_tokens or 0 for item in ready_items
            ),
            "unready_cases": len(unready),
            "expansion_frequency": (
                expansion_success_cases / len(eligible_expansion)
                if eligible_expansion
                else None
            ),
        }
    return summary


def normalize_run(run: ArmRun) -> dict[str, Any]:
    payload = {
        "case_id": run.case_id,
        "arm": run.arm,
        "classification": run.classification,
        "profile": run.profile,
        "ready": run.ready,
        "status": run.status,
        "reason_codes": list(run.reason_codes),
        "delivered_intervals": list(run.delivered_intervals),
        "anchor_covered": run.anchor_covered,
        "must_cover_hits": run.must_cover_hits,
        "must_cover_misses": run.must_cover_misses,
        "missing_dependency_count": run.missing_dependency_count,
        "forbidden_emissions": run.forbidden_emissions,
        "wire_input_tokens": run.wire_input_tokens,
        "wire_total_tokens": run.wire_total_tokens,
        "native_input_tokens": run.native_input_tokens,
        "native_output_tokens": run.native_output_tokens,
        "native_total_tokens": run.native_total_tokens,
        "dependency_attributed_tokens": run.dependency_attributed_tokens,
        "expansion_attempts": run.expansion_attempts,
        "expansion_successes": run.expansion_successes,
        "expansion_applicable": run.expansion_applicable,
        "scope_paths": list(run.scope_paths),
        "notes": list(run.notes),
    }
    missing = [key for key in REQUIRED_NON_TIMING if key not in payload]
    if missing:
        raise RuntimeError(f"arm run missing comparability fields: {missing}")
    if not run.case_id:
        raise RuntimeError("empty fixture identity")
    return payload


def run_suite(
    arms: Sequence[ArmName],
    *,
    latency_repeats: int = 0,
    include_latency: bool = False,
) -> ScoreSummary:
    unknown = [arm for arm in arms if arm not in KNOWN_ARMS]
    if unknown:
        raise ValueError(f"unknown arms: {unknown}")
    if "candidate" in arms and "v2" not in arms and "v1" not in arms:
        # candidate is allowed alone for later phases; still validate identity.
        pass
    digest = fixture_hash()
    if not digest:
        raise RuntimeError("empty fixture identity")
    runs: list[ArmRun] = []
    for case in DEV_CASES:
        for arm in arms:
            with tempfile.TemporaryDirectory(prefix=f"lw-dev-{case.case_id}-") as tmp:
                base = Path(tmp)
                workspace = base / "repo"
                cache = base / "cache"
                workspace.mkdir()
                cache.mkdir(mode=0o700)
                run = run_case_arm(case, arm, workspace=workspace, cache_base=cache)
                runs.append(run)
                log.debug(
                    "[DevEval] recorded case=%s arm=%s ready=%s eligibility=coverage=%s",
                    case.case_id,
                    arm,
                    run.ready,
                    case.classification == "coverage",
                )
    latency: dict[str, Any] = {}
    if include_latency and latency_repeats > 0:
        latency["cold"] = []
        latency["warm"] = []
        for case in DEV_CASES:
            for arm in arms:
                latency["cold"].append(
                    measure_latency(case, arm, repeats=latency_repeats, warm=False)
                )
                latency["warm"].append(
                    measure_latency(case, arm, repeats=latency_repeats, warm=True)
                )
    summary = ScoreSummary(
        fixture_hash=digest,
        baseline_revision=BASELINE_REVISION,
        arms=tuple(arms),
        case_runs=runs,
        aggregates=aggregate_runs(runs),
        latency=latency,
        environment={
            "estimator": ConservativeByteCounter.name,
            "model": COMMON_MODEL,
            "output_reserve": COMMON_OUTPUT_RESERVE,
            "representational_notes": {
                "v1": "DeterministicContextProvider.build('implement') RANGES excerpts + messages",
                "v2": "ContextPacker.open_task with required_paths edit target; dependency coverage disabled",
                "candidate": (
                    "Same V2 packer/request/scope as control with "
                    "dependency_coverage.enabled=true (defaults: depth=2, defs=8, tokens=1024)"
                ),
                "wire": "ConservativeByteCounter.count_request on common canonical envelope",
                "expansion": "V1 expansion_applicable=null; V2 label-free registered-candidate policy",
            },
        },
    )
    return summary


def _index_runs(runs: Sequence[ArmRun]) -> dict[tuple[str, str], ArmRun]:
    return {(run.case_id, run.arm): run for run in runs}


def token_overhead_allowance(
    *,
    v2_input: int,
    newly_covered: int,
) -> int:
    if newly_covered <= 0:
        return max(64, math.ceil(0.05 * v2_input))
    return math.ceil(0.25 * v2_input) + 128 * newly_covered


def evaluate_candidate_gates(
    summary: ScoreSummary,
    *,
    baseline: dict[str, Any] | None = None,
) -> list[str]:
    """Deterministic development-set gates for the candidate arm.

    Compares against live V1/V2 arms in ``summary`` and optionally verifies the
    frozen V1/V2 baseline still matches when provided. Timing flags are returned
    as ``timing_warning:`` prefixes and do not fail the gate by themselves until
    corroborated by a second development-only run.
    """

    errors: list[str] = []
    indexed = _index_runs(summary.case_runs)
    if "candidate" not in summary.arms:
        errors.append("candidate arm missing from summary")
        return errors
    if "v2" not in summary.arms:
        errors.append("v2 control arm required for candidate gates")
        return errors
    if "v1" not in summary.arms:
        errors.append("v1 reference arm required for candidate gates")
        return errors

    if baseline is not None:
        control = ScoreSummary(
            fixture_hash=summary.fixture_hash,
            baseline_revision=summary.baseline_revision,
            arms=("v1", "v2"),
            case_runs=[
                run for run in summary.case_runs if run.arm in {"v1", "v2"}
            ],
        )
        control_errors = compare_to_baseline(control, baseline)
        for item in control_errors:
            errors.append(f"baseline_parity:{item}")

    strict_gains = 0
    missing_candidate = 0
    missing_v1 = 0
    missing_v2 = 0
    ready_coverage_control_v2_input = 0
    ready_coverage_control_candidate_input = 0
    ready_final_v2_input = 0
    ready_final_candidate_input = 0

    for case in DEV_CASES:
        cand = indexed.get((case.case_id, "candidate"))
        v2 = indexed.get((case.case_id, "v2"))
        v1 = indexed.get((case.case_id, "v1"))
        if cand is None or v2 is None or v1 is None:
            errors.append(f"{case.case_id}: missing paired arm rows")
            continue

        if case.classification in {"budget", "uncertainty"}:
            # Intentional control statuses must not silently flip to ready success.
            if cand.ready != v2.ready and cand.status not in {
                "insufficient_context",
                "stale_reference",
                "ready",
            }:
                errors.append(
                    f"{case.case_id}: unexpected candidate status {cand.status!r}"
                )
            # Still count missing for suite totals when coverage labels exist.
        if case.classification == "coverage":
            missing_candidate += cand.missing_dependency_count
            missing_v1 += v1.missing_dependency_count
            missing_v2 += v2.missing_dependency_count
            if cand.must_cover_hits < v2.must_cover_hits:
                errors.append(
                    f"{case.case_id}: coverage regresses vs v2 "
                    f"({cand.must_cover_hits}<{v2.must_cover_hits})"
                )
            if cand.must_cover_hits < v1.must_cover_hits:
                errors.append(
                    f"{case.case_id}: coverage regresses vs v1 "
                    f"({cand.must_cover_hits}<{v1.must_cover_hits})"
                )
            # Anchors: never lose ground versus current V2; also hold every
            # anchor V1 already held when V2 already held it.
            if cand.anchor_covered < v2.anchor_covered:
                errors.append(
                    f"{case.case_id}: required anchor regresses vs v2 "
                    f"({cand.anchor_covered}<{v2.anchor_covered})"
                )
            if (
                v2.anchor_covered >= v1.anchor_covered
                and cand.anchor_covered < v1.anchor_covered
            ):
                errors.append(
                    f"{case.case_id}: required anchor regresses vs v1 "
                    f"({cand.anchor_covered}<{v1.anchor_covered})"
                )
            forbidden_ceiling = max(v1.forbidden_emissions, v2.forbidden_emissions)
            if cand.forbidden_emissions > forbidden_ceiling:
                errors.append(
                    f"{case.case_id}: forbidden emissions worsened "
                    f"({cand.forbidden_emissions}>{forbidden_ceiling})"
                )
            newly = cand.must_cover_hits - v2.must_cover_hits
            if newly > 0:
                strict_gains += 1
            if cand.ready and v2.ready:
                v2_in = int(v2.wire_input_tokens or 0)
                cand_in = int(cand.wire_input_tokens or 0)
                allowance = token_overhead_allowance(
                    v2_input=v2_in, newly_covered=max(0, newly)
                )
                if cand_in > v2_in + allowance:
                    errors.append(
                        f"{case.case_id}: input overhead {cand_in - v2_in} "
                        f"exceeds allowance {allowance}"
                    )
                ready_coverage_control_v2_input += v2_in
                ready_coverage_control_candidate_input += cand_in
                ready_final_v2_input += v2_in
                ready_final_candidate_input += cand_in
            elif cand.ready and not v2.ready:
                # Baseline refused: disclose without using zero input as comparator.
                log.debug(
                    "[DevEval] gate_note case=%s v2_unready candidate_ready",
                    case.case_id,
                )
            elif not cand.ready and v2.ready:
                errors.append(f"{case.case_id}: candidate unready while v2 ready")

        if case.classification == "control" and cand.ready and v2.ready:
            v2_in = int(v2.wire_input_tokens or 0)
            cand_in = int(cand.wire_input_tokens or 0)
            newly = max(0, cand.must_cover_hits - v2.must_cover_hits)
            allowance = token_overhead_allowance(
                v2_input=v2_in, newly_covered=newly
            )
            if cand_in > v2_in + allowance:
                errors.append(
                    f"{case.case_id}: control input overhead "
                    f"{cand_in - v2_in} exceeds allowance {allowance}"
                )
            ready_coverage_control_v2_input += v2_in
            ready_coverage_control_candidate_input += cand_in
            ready_final_v2_input += v2_in
            ready_final_candidate_input += cand_in

    if strict_gains < 1:
        errors.append("no strict coverage gain versus current v2")
    if missing_candidate > missing_v1 or missing_candidate > missing_v2:
        errors.append(
            "suite missing_dependency_total worse than a baseline arm "
            f"(candidate={missing_candidate} v1={missing_v1} v2={missing_v2})"
        )
    if ready_coverage_control_v2_input > 0:
        ceiling = int(ready_coverage_control_v2_input * 1.20)
        if ready_coverage_control_candidate_input > ceiling:
            errors.append(
                "suite ready input sum exceeds 20% over v2 "
                f"({ready_coverage_control_candidate_input}>{ceiling})"
            )
    if ready_final_v2_input > 0:
        newly_suite = max(0, missing_v2 - missing_candidate)
        allowance = token_overhead_allowance(
            v2_input=ready_final_v2_input, newly_covered=newly_suite
        )
        if ready_final_candidate_input > ready_final_v2_input + allowance:
            errors.append(
                "suite final input overhead exceeds allowance "
                f"({ready_final_candidate_input - ready_final_v2_input}>{allowance})"
            )

    # Timing review flag (non-fatal unless caller promotes it).
    latency = summary.latency or {}
    warm = latency.get("warm") or []
    by_case: dict[str, dict[str, Any]] = {}
    for sample in warm:
        by_case.setdefault(sample["case_id"], {})[sample["arm"]] = sample
    for case_id, arms in by_case.items():
        cand_sample = arms.get("candidate")
        v2_sample = arms.get("v2")
        if not cand_sample or not v2_sample:
            continue
        cand_p95 = float(cand_sample["p95_ms"])
        v2_p95 = float(v2_sample["p95_ms"])
        if cand_p95 > max(v2_p95 * 1.5, v2_p95 + 10.0):
            errors.append(
                f"timing_warning:{case_id} warm_p95 "
                f"candidate={cand_p95:.3f} v2={v2_p95:.3f}"
            )

    log.debug(
        "[DevEval] gates strict_gains=%s missing_c=%s missing_v2=%s errors=%s",
        strict_gains,
        missing_candidate,
        missing_v2,
        len(errors),
    )
    return errors


def gate_failures(errors: Sequence[str]) -> list[str]:
    """Strip advisory timing warnings for exit-code decisions."""

    return [item for item in errors if not item.startswith("timing_warning:")]


def summary_to_jsonable(summary: ScoreSummary, *, include_timing: bool) -> dict[str, Any]:
    payload = {
        "fixture_hash": summary.fixture_hash,
        "baseline_revision": summary.baseline_revision,
        "arms": list(summary.arms),
        "fixture_manifest_digest": fixture_hash(),
        "aggregates": summary.aggregates,
        "environment": summary.environment,
        "cases": [normalize_run(run) for run in summary.case_runs],
    }
    if include_timing:
        payload["latency"] = summary.latency
    return payload


def freeze_baseline_payload(summary: ScoreSummary) -> dict[str, Any]:
    """Persist non-timing comparable measurements with exact fixture identity."""

    return {
        "kind": "retrieval_dependencies_dev_baseline",
        "version": 1,
        "fixture_hash": summary.fixture_hash,
        "baseline_revision": summary.baseline_revision,
        "arms": list(summary.arms),
        "profiles": {name: asdict(profile) for name, profile in sorted(PROFILES.items())},
        "environment": summary.environment,
        "aggregates": summary.aggregates,
        "cases": [normalize_run(run) for run in summary.case_runs],
        "manifest": fixture_manifest(),
    }


def load_baseline(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline must be an object")
    for key in ("fixture_hash", "baseline_revision", "cases", "arms"):
        if key not in payload:
            raise ValueError(f"baseline missing {key}")
    return payload


def compare_to_baseline(
    summary: ScoreSummary,
    baseline: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if summary.fixture_hash != baseline["fixture_hash"]:
        errors.append("fixture_hash mismatch")
    if summary.baseline_revision != baseline["baseline_revision"]:
        errors.append("baseline_revision mismatch")
    expected_arms = tuple(baseline["arms"])
    if summary.arms != expected_arms:
        errors.append(f"arms mismatch: {summary.arms!r} != {expected_arms!r}")
    current = {
        (item["case_id"], item["arm"]): item
        for item in (normalize_run(run) for run in summary.case_runs)
    }
    frozen = {(item["case_id"], item["arm"]): item for item in baseline["cases"]}
    if set(current) != set(frozen):
        errors.append("case/arm set mismatch")
        return errors
    for key, expected in frozen.items():
        actual = current[key]
        for field_name in REQUIRED_NON_TIMING:
            if actual.get(field_name) != expected.get(field_name):
                errors.append(f"{key[0]}/{key[1]} field {field_name} drifted")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Development-only retrieval dependency scorecard (no holdout corpus)."
    )
    parser.add_argument(
        "--arms",
        required=True,
        help="Comma-separated arms: v1,v2[,candidate]",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--latency-repeats",
        type=int,
        default=21,
        help="Latency repeats after one warm-up (0 disables latency block).",
    )
    parser.add_argument(
        "--freeze-baseline",
        action="store_true",
        help="Write non-timing baseline payload (excludes absolute paths and timings).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Frozen v1/v2 baseline JSON for parity checks when candidate is gated.",
    )
    parser.add_argument(
        "--skip-gates",
        action="store_true",
        help="Write the scorecard without evaluating candidate gates.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    arms = tuple(item.strip() for item in args.arms.split(",") if item.strip())
    unknown = [arm for arm in arms if arm not in KNOWN_ARMS]
    if unknown:
        print(f"unknown arms: {unknown}", file=sys.stderr)
        return 2
    if args.output.exists():
        print(f"refusing to overwrite existing output: {args.output}", file=sys.stderr)
        return 2
    include_latency = args.latency_repeats > 0 and not args.freeze_baseline
    summary = run_suite(
        arms,
        latency_repeats=args.latency_repeats if include_latency else 0,
        include_latency=include_latency,
    )
    gate_errors: list[str] = []
    if "candidate" in arms and not args.freeze_baseline and not args.skip_gates:
        baseline_payload = None
        baseline_path = args.baseline
        if baseline_path is None:
            default_baseline = Path(__file__).with_name("dev_baseline.json")
            if default_baseline.exists():
                baseline_path = default_baseline
        if baseline_path is not None:
            baseline_payload = load_baseline(baseline_path)
        gate_errors = evaluate_candidate_gates(summary, baseline=baseline_payload)
    if args.freeze_baseline:
        payload = freeze_baseline_payload(summary)
    else:
        payload = summary_to_jsonable(summary, include_timing=include_latency)
        if "candidate" in arms and not args.skip_gates:
            fatal = gate_failures(gate_errors)
            payload["gates"] = {
                "ok": not fatal,
                "errors": list(gate_errors),
                "fatal": fatal,
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fatal = gate_failures(gate_errors)
    log.info(
        "[DevEval] wrote output=%s arms=%s fixture_hash=%s freeze=%s fatal_gates=%s",
        args.output,
        arms,
        summary.fixture_hash,
        args.freeze_baseline,
        len(fatal),
    )
    if fatal:
        for item in fatal:
            print(item, file=sys.stderr)
        return 1
    for item in gate_errors:
        if item.startswith("timing_warning:"):
            print(item, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
