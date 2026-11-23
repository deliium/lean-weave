#!/usr/bin/env python3
"""Custodian-only blind scoring for V2 release evidence.

Oracle labels are read only by this scorer after capture hashes are sealed.
Capture workers must never import this module's scoring entry points with live
labels during a blind run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

log = logging.getLogger("lean_weave.release_blind_score")

SCHEMA_BLIND_RECEIPT = "lw-blind-receipt-v1"
SCHEMA_FREEZE = "lw-blind-freeze-v1"


def _configure_logging() -> None:
    selected = (
        os.getenv("LEAN_WEAVE_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    ).upper()
    level = getattr(logging, selected, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_digest(payload: Mapping[str, Any] | list[Any] | str) -> str:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    return sha256_bytes(encoded)


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("json_root_not_object")
    return data


def validate_suite(suite: Mapping[str, Any]) -> list[str]:
    """Validate freeze-time suite schema without looking at candidate output."""

    errors: list[str] = []
    cases = suite.get("cases")
    if not isinstance(cases, list) or len(cases) < 12:
        errors.append("insufficient_cases")
    repos = {str(item.get("repository")) for item in (cases or []) if isinstance(item, dict)}
    if len(repos) < 3:
        errors.append("insufficient_repositories")
    heavy = [
        item
        for item in (cases or [])
        if isinstance(item, dict) and item.get("dependency_heavy")
    ]
    if len(heavy) < 8:
        errors.append("insufficient_dependency_heavy")
    for item in heavy:
        labels = item.get("required_dependencies") or []
        if len(labels) < 2:
            errors.append("dependency_heavy_underlabeled")
            break
    for item in cases or []:
        if not isinstance(item, dict):
            errors.append("case_not_object")
            continue
        files = item.get("relevant_files") or []
        symbols = item.get("relevant_symbols") or []
        deps = item.get("required_dependencies") or []
        if not files and not symbols and not deps:
            errors.append("empty_denominator")
        seen_ids: set[str] = set()
        for file_id in files:
            if file_id in seen_ids:
                errors.append("duplicate_file_id")
            seen_ids.add(str(file_id))
    return list(dict.fromkeys(errors))


def union_spans(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Union overlapping/adjacent spans for the same path and digest."""

    by_key: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for span in spans:
        key = (str(span["path"]), str(span.get("text_digest") or ""))
        by_key.setdefault(key, []).append((int(span["start_line"]), int(span["end_line"])))
    merged: list[dict[str, Any]] = []
    for (path, digest), ranges in sorted(by_key.items()):
        ordered = sorted(ranges)
        current_start, current_end = ordered[0]
        for start, end in ordered[1:]:
            if start <= current_end + 1:
                current_end = max(current_end, end)
            else:
                merged.append(
                    {
                        "path": path,
                        "start_line": current_start,
                        "end_line": current_end,
                        "text_digest": digest,
                    }
                )
                current_start, current_end = start, end
        merged.append(
            {
                "path": path,
                "start_line": current_start,
                "end_line": current_end,
                "text_digest": digest,
            }
        )
    return merged


def interval_covered(
    spans: Sequence[Mapping[str, Any]],
    *,
    path: str,
    start_line: int,
    end_line: int,
) -> bool:
    for span in union_spans(spans):
        if span["path"] != path:
            continue
        if int(span["start_line"]) <= start_line and int(span["end_line"]) >= end_line:
            return True
    return False


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    top = list(ranked)[:k]
    if k <= 0:
        return 0.0
    hits = sum(1 for item in top if item in relevant)
    return hits / float(k)


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = list(ranked)[:k]
    hits = sum(1 for item in top if item in relevant)
    return hits / float(len(relevant))


def _symbol_key(item: Mapping[str, Any] | Sequence[Any]) -> str:
    if isinstance(item, Mapping):
        path = str(item["path"])
        name = str(item.get("qualified_name") or item.get("name"))
        start = int(item.get("definition_start_line") or item.get("start_line"))
    else:
        path = str(item[0])
        name = str(item[1])
        start = int(item[2])
    return f"{path}::{name}::{start}"


def rank_covered_files(
    spans: Sequence[Mapping[str, Any]],
    relevant: Sequence[str],
) -> list[str]:
    """Order fully covered relevant files by earliest contributing span."""

    covered: list[tuple[int, str]] = []
    for file_id in relevant:
        earliest = None
        for index, span in enumerate(spans):
            if span["path"] == file_id and interval_covered(
                spans,
                path=str(file_id),
                start_line=int(span["start_line"]),
                end_line=int(span["end_line"]),
            ):
                # Presence alone is insufficient; require at least one covered interval.
                earliest = index if earliest is None else min(earliest, index)
        if earliest is None:
            for index, span in enumerate(spans):
                if span["path"] == file_id:
                    earliest = index if earliest is None else min(earliest, index)
        if earliest is not None:
            covered.append((earliest, file_id))
    covered.sort(key=lambda item: (item[0], item[1]))
    return [item[1] for item in covered]


def rank_covered_symbols(
    spans: Sequence[Mapping[str, Any]],
    relevant_symbols: Sequence[Mapping[str, Any] | Sequence[Any]],
    delivered_symbol_ids: Sequence[Sequence[Any]] | None = None,
) -> list[str]:
    """Rank symbols whose declaration header line is fully covered."""

    covered: list[tuple[int, str]] = []
    delivered = {
        _symbol_key(item)
        for item in (delivered_symbol_ids or [])
    }
    for symbol in relevant_symbols:
        key = _symbol_key(symbol)
        if isinstance(symbol, Mapping):
            path = str(symbol["path"])
            start = int(symbol.get("definition_start_line") or symbol.get("start_line"))
            end = int(symbol.get("end_line") or start)
        else:
            path = str(symbol[0])
            start = int(symbol[2])
            end = start
        if not interval_covered(spans, path=path, start_line=start, end_line=end):
            continue
        if delivered and key not in delivered:
            # Map-only / non-source matches never enter the rank.
            continue
        earliest = None
        for index, span in enumerate(spans):
            if span["path"] != path:
                continue
            if int(span["start_line"]) <= start and int(span["end_line"]) >= end:
                earliest = index if earliest is None else min(earliest, index)
        if earliest is not None:
            covered.append((earliest, key))
    covered.sort(key=lambda item: (item[0], item[1]))
    return [item[1] for item in covered]


def score_pair(
    *,
    capture: Mapping[str, Any],
    labels: Mapping[str, Any],
) -> dict[str, Any]:
    """Score V1/V2 capture records against custodian labels."""

    log.info("score_pair case=%s", capture.get("case_id"))
    by_arm = {item["arm"]: item for item in capture.get("records") or []}
    if "v1" not in by_arm or "v2" not in by_arm:
        return {
            "ok": False,
            "reason_codes": ["pair_incomplete", "holdout_invalid"],
            "gates": {},
        }
    relevant_files = [str(item) for item in labels.get("relevant_files") or []]
    relevant_symbols = list(labels.get("relevant_symbols") or [])
    required_deps = labels.get("required_dependencies") or []
    edit_anchor = labels.get("edit_anchor") or {}
    results: dict[str, Any] = {
        "arms": {},
        "gates": {},
        "reason_codes": [],
        "latency": {},
    }
    latency_values: list[float] = []

    for arm, record in by_arm.items():
        spans = record.get("spans") or []
        if record.get("status") not in {"ok", "delivered"}:
            results["arms"][arm] = {
                "status": record.get("status"),
                "missing_dependencies": None,
                "file_recall_at_5": None,
                "file_precision_at_5": None,
                "symbol_recall_at_5": None,
                "symbol_precision_at_5": None,
            }
            results["reason_codes"].append("pair_incomplete")
            continue
        ranked_files = rank_covered_files(spans, relevant_files)
        ranked_symbols = rank_covered_symbols(
            spans,
            relevant_symbols,
            delivered_symbol_ids=record.get("symbol_ids") or [],
        )
        missing = []
        for dep in required_deps:
            if not interval_covered(
                spans,
                path=str(dep["path"]),
                start_line=int(dep["start_line"]),
                end_line=int(dep["end_line"]),
            ):
                missing.append(dep)
        anchor_ok = False
        if edit_anchor:
            anchor_ok = interval_covered(
                spans,
                path=str(edit_anchor["path"]),
                start_line=int(edit_anchor["start_line"]),
                end_line=int(edit_anchor["end_line"]),
            )
        relevant_symbol_keys = {_symbol_key(item) for item in relevant_symbols}
        timing = record.get("timing_ms") or {}
        total_ms = timing.get("total")
        if isinstance(total_ms, (int, float)):
            latency_values.append(float(total_ms))
        results["arms"][arm] = {
            "status": record.get("status"),
            "missing_dependencies": len(missing),
            "required_dependencies": len(required_deps),
            "edit_anchor_covered": anchor_ok,
            "file_recall_at_5": recall_at_k(ranked_files, set(relevant_files), 5),
            "file_precision_at_5": precision_at_k(ranked_files, set(relevant_files), 5),
            "symbol_recall_at_5": recall_at_k(ranked_symbols, relevant_symbol_keys, 5),
            "symbol_precision_at_5": precision_at_k(
                ranked_symbols, relevant_symbol_keys, 5
            ),
            "context_bytes": record.get("context_bytes"),
            "estimated_input_tokens": record.get("estimated_input_tokens"),
            "timing_ms": timing,
        }

    v1 = results["arms"].get("v1") or {}
    v2 = results["arms"].get("v2") or {}
    gates = {
        "paired_delivery": v1.get("status") in {"ok", "delivered"}
        and v2.get("status") in {"ok", "delivered"},
        "edit_anchor_both": bool(v1.get("edit_anchor_covered"))
        and bool(v2.get("edit_anchor_covered")),
        "v2_zero_required_misses": v2.get("missing_dependencies") == 0,
        "no_file_recall_loss": (
            v2.get("file_recall_at_5") is not None
            and v1.get("file_recall_at_5") is not None
            and v2["file_recall_at_5"] >= v1["file_recall_at_5"]
        ),
        "no_symbol_recall_loss": (
            not relevant_symbols
            or (
                v2.get("symbol_recall_at_5") is not None
                and v1.get("symbol_recall_at_5") is not None
                and v2["symbol_recall_at_5"] >= v1["symbol_recall_at_5"]
            )
        ),
    }
    results["gates"] = gates
    results["latency"] = aggregate_latencies(latency_values)
    if not gates["paired_delivery"]:
        results["reason_codes"].append("pair_incomplete")
    if not gates["v2_zero_required_misses"]:
        results["reason_codes"].append("dependency_gate_failed")
    if not gates["no_file_recall_loss"] or not gates["no_symbol_recall_loss"]:
        results["reason_codes"].append("recall_gate_failed")
    results["ok"] = all(gates.values()) and not results["reason_codes"]
    return results


def retire_corpus_marker(path: Path, *, reason: str = "disclosed") -> dict[str, Any]:
    """Write a create-once retirement marker after candidate-visible disclosure."""

    if path.exists():
        raise FileExistsError("duplicate_retirement")
    payload = {
        "schema": "lw-blind-corpus-retired-v1",
        "retired": True,
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def aggregate_latencies(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "median": None, "p95": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "count": len(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[index],
    }


def verify_ed25519_detached(
    *,
    openssl: str,
    public_key: Path,
    message: Path,
    signature: Path,
) -> bool:
    """Verify a detached Ed25519 signature with a pinned openssl binary."""

    log.debug("ed25519_verify key=%s", public_key.name)
    completed = subprocess.run(
        [
            openssl,
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(public_key),
            "-rawin",
            "-in",
            str(message),
            "-sigfile",
            str(signature),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def build_blind_receipt(
    *,
    run_id: str,
    artifact_id: str,
    commit: str | None,
    wheel_sha256: str | None,
    suite_digest: str,
    capture_digest: str,
    score: Mapping[str, Any],
    schedule_digest: str,
) -> dict[str, Any]:
    status = "pass" if score.get("ok") else "fail"
    return {
        "schema": SCHEMA_BLIND_RECEIPT,
        "envelope": {
            "schema": "lw-evidence-envelope-v1",
            "run_id": run_id,
            "stage_id": "blind",
            "artifact_id": artifact_id,
            "commit": commit,
            "wheel_sha256": wheel_sha256,
            "config_digest": None,
            "dataset_digest": suite_digest,
            "runner_mode": "installed-wheel",
            "output_hashes": {
                "capture": capture_digest,
                "schedule": schedule_digest,
            },
            "status": status,
            "reason_codes": list(score.get("reason_codes") or []),
        },
        "gates": dict(score.get("gates") or {}),
        "arms": dict(score.get("arms") or {}),
    }


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="release_blind_score")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-suite")
    validate.add_argument("--suite", required=True)

    score_cmd = sub.add_parser("score")
    score_cmd.add_argument("--capture", required=True)
    score_cmd.add_argument("--labels", required=True)
    score_cmd.add_argument("--output", required=True)
    score_cmd.add_argument("--run-id", required=True)
    score_cmd.add_argument("--artifact-id", required=True)
    score_cmd.add_argument("--commit", default=None)
    score_cmd.add_argument("--wheel-sha256", default=None)
    score_cmd.add_argument("--suite-digest", required=True)
    score_cmd.add_argument("--schedule-digest", required=True)

    verify_sig = sub.add_parser("verify-signature")
    verify_sig.add_argument("--openssl", default="openssl")
    verify_sig.add_argument("--public-key", required=True)
    verify_sig.add_argument("--message", required=True)
    verify_sig.add_argument("--signature", required=True)

    retire = sub.add_parser("retire-corpus")
    retire.add_argument("--marker", required=True)
    retire.add_argument("--reason", default="disclosed")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "validate-suite":
        suite = _load_json(Path(args.suite).resolve())
        errors = validate_suite(suite)
        if errors:
            errors = list(dict.fromkeys([*errors, "holdout_invalid"]))
        print(json.dumps({"ok": not errors, "errors": errors}, sort_keys=True))
        return 0 if not errors else 1

    if args.command == "verify-signature":
        ok = verify_ed25519_detached(
            openssl=str(args.openssl),
            public_key=Path(args.public_key).resolve(),
            message=Path(args.message).resolve(),
            signature=Path(args.signature).resolve(),
        )
        print(json.dumps({"ok": ok}, sort_keys=True))
        return 0 if ok else 1

    if args.command == "retire-corpus":
        try:
            retire_corpus_marker(Path(args.marker).resolve(), reason=str(args.reason))
        except FileExistsError:
            print(json.dumps({"ok": False, "error": "duplicate_retirement"}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "retired": True}, sort_keys=True))
        return 0

    capture = _load_json(Path(args.capture).resolve())
    labels = _load_json(Path(args.labels).resolve())
    scored = score_pair(capture=capture, labels=labels)
    receipt = build_blind_receipt(
        run_id=args.run_id,
        artifact_id=args.artifact_id,
        commit=args.commit,
        wheel_sha256=args.wheel_sha256,
        suite_digest=args.suite_digest,
        capture_digest=sha256_file(Path(args.capture).resolve()),
        score=scored,
        schedule_digest=args.schedule_digest,
    )
    receipt["latency"] = scored.get("latency") or {}
    out = Path(args.output).resolve()
    if out.exists():
        log.error("duplicate_blind_receipt")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": scored.get("ok"), "status": receipt["envelope"]["status"]}, sort_keys=True))
    return 0 if scored.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
