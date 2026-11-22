"""Human-readable benchmark reports over versioned run/comparison results."""

from __future__ import annotations

import json
from typing import Any, Mapping

from lean_weave.benchmark.models import BenchmarkRun


def _label(value: Any) -> str:
    text = "unavailable" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _fmt(value: Any) -> str:
    return "unavailable" if value is None else str(value)


def _provenance_label(source: str) -> str:
    return {
        "offline_scripted": "mock/scripted",
        "local_estimate": "local estimate",
        "deepseek_live": "live provider-reported",
        "historical": "historical",
    }.get(source, source)


def render_run_markdown(run: BenchmarkRun | Mapping[str, Any]) -> str:
    payload = run.to_dict() if isinstance(run, BenchmarkRun) else dict(run)
    lines = [
        f"# Benchmark run: {_label(payload.get('suite'))}",
        "",
        f"- Schema: {_label(payload.get('schema_version'))}",
        f"- Evidence: {_provenance_label(str(payload.get('evidence_source')))}",
        f"- Revision: {_label(payload.get('revision'))}",
        f"- Source revision: {_label(payload.get('source_revision'))}",
        f"- Started: {_label(payload.get('started_at'))}",
        f"- Incomplete: {_label(payload.get('incomplete'))}",
        "",
        "## Arms",
        "",
        "| Case | Variant | Rep | Status | Correct | Negative | Oracle | Tokens | "
        "Calls | Retries | Dispatch | Duration |",
        "| --- | --- | ---: | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for row in payload.get("rows", []):
        evidence = row.get("evidence") or {}
        lines.append(
            "| {case} | {variant} | {rep} | {status} | {correct} | {negative} | "
            "{oracle} | {tokens} | {calls} | {retries} | {dispatch} | {duration} |".format(
                case=_label(row.get("case_id")),
                variant=_label(row.get("variant")),
                rep=_label(row.get("repetition")),
                status=_label(row.get("status")),
                correct=_label(row.get("correct")),
                negative=_label(row.get("expected_negative")),
                oracle=_label(row.get("oracle_ok")),
                tokens=_fmt(row.get("total_tokens")),
                calls=_fmt(row.get("logical_calls")),
                retries=_fmt(row.get("workflow_retries")),
                dispatch=_label(evidence.get("dispatch")),
                duration=_fmt(row.get("duration_ms")),
            )
        )
    metrics = payload.get("metrics") or {}
    if metrics:
        lines.extend(["", "## Aggregate metrics"])
        lines.extend(f"- {_label(key)}: {_fmt(value)}" for key, value in sorted(metrics.items()) if key != "input_components")
        components = metrics.get("input_components") or {}
        if components.get("components"):
            lines.extend(
                [
                    "",
                    "## Estimated input components",
                    "",
                    f"- Arms with evidence: {_label(components.get('arms'))}",
                    f"- Sent requests / unsent reservations: {_label(components.get('sent_requests'))} / {_label(components.get('unsent_reservations'))}",
                    "",
                    "| Component | Tokens |",
                    "| --- | ---: |",
                ]
            )
            for key, value in sorted(components["components"].items()):
                lines.append(f"| {_label(key)} | {_fmt(value)} |")
            lines.append("")
            lines.append(
                "Components are explanatory slices counted with the packing estimator; they are not assumed to sum to the complete request estimate."
            )
    if payload.get("notes"):
        lines.extend(["", "## Notes"])
        lines.extend(f"- {_label(note)}" for note in payload["notes"])
    return "\n".join(lines) + "\n"


def render_comparison_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Benchmark comparison ({_label(payload.get('axis'))})",
        "",
        f"- Comparable: {_label(payload.get('comparable'))}",
        f"- Baseline variant: {_label(payload.get('baseline_variant'))}",
        f"- Candidate variant: {_label(payload.get('candidate_variant'))}",
        f"- Scheduled arms: {_label(payload.get('scheduled'))}",
        f"- Correct baseline/candidate: {payload.get('baseline_correct', 0)}/"
        f"{payload.get('candidate_correct', 0)}",
    ]
    metrics = payload.get("metrics") or {}
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    for key in sorted(metrics):
        lines.append(f"| {_label(key)} | {_fmt(metrics[key])} |")
    if payload.get("reasons"):
        lines.extend(["", "## Findings"])
        lines.extend(f"- {_label(reason)}" for reason in payload["reasons"])
    if payload.get("gates"):
        lines.extend(
            [
                "",
                "## Gates",
                "",
                "| Metric | Status | Observed | Max regression |",
                "| --- | --- | ---: | ---: |",
            ]
        )
        for gate in payload["gates"]:
            lines.append(
                f"| {_label(gate.get('metric'))} | {_label(gate.get('status'))} | "
                f"{_fmt(gate.get('observed'))} | {_fmt(gate.get('max_regression'))} |"
            )
    return "\n".join(lines) + "\n"


def render_run_json(run: BenchmarkRun | Mapping[str, Any]) -> str:
    payload = run.to_dict() if isinstance(run, BenchmarkRun) else dict(run)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
