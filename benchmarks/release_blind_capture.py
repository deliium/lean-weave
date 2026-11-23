#!/usr/bin/env python3
"""Label-free blind capture worker for V2 release evidence.

The capture process receives only task text, edit target, pinned repository,
arm config and an opaque case ID. It must never open oracle labels.

Synthetic mode is for protocol tests. Installed mode invokes an isolated
``python -I -B`` worker against the verified wheel (or an explicit stub).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

log = logging.getLogger("lean_weave.release_blind_capture")

try:
    from lean_weave.app import AppContext, create_code_index, create_context_packer
    from lean_weave.code_index import CodeIndex
    from lean_weave.config.models import (
        AppConfig,
        ContextConfig,
        DependencyCoverageConfig,
        PackingConfig,
    )
    from lean_weave.context.packing_models import PackRequest
    from lean_weave.context.provider import DeterministicContextProvider
    from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage

    HAS_PRODUCT = True
except ImportError:  # pragma: no cover - stdlib-only extract path
    HAS_PRODUCT = False


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


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("json_root_not_object")
    return data


def assert_no_oracle(payload: Mapping[str, Any]) -> None:
    forbidden = {
        "oracle",
        "labels",
        "required_dependencies",
        "relevant_files",
        "relevant_symbols",
        "expected_spans",
    }
    hits = forbidden.intersection(payload)
    if hits:
        raise ValueError(f"oracle_leak:{sorted(hits)}")


def extract_delivered_spans(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize delivered source units into path/start/end/digest records."""

    spans: list[dict[str, Any]] = []
    for unit in units:
        path = str(unit["path"])
        start = int(unit["start_line"])
        end = int(unit["end_line"])
        text = str(unit.get("text") or "")
        spans.append(
            {
                "path": path,
                "start_line": start,
                "end_line": end,
                "text_digest": sha256_bytes(text.encode("utf-8")),
                "byte_len": len(text.encode("utf-8")),
            }
        )
    return spans


def symbol_ids_from_source(path: str, source: str) -> list[tuple[str, str, int]]:
    """Return (path, qualified_name, definition_start_line) for top-level defs."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[str, str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((path, node.name, int(node.lineno)))
    return found


def fixture_inventory_digest(root: Path, relative_files: Sequence[str]) -> str:
    """Digest a declared fixture file inventory for before/after checks."""

    rows: list[dict[str, str]] = []
    for relative in sorted(relative_files):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"fixture_file_invalid:{relative}")
        rows.append({"path": relative, "digest": sha256_file(path)})
    return sha256_bytes(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def capture_arm(
    *,
    case_id: str,
    arm: str,
    request: str,
    edit_target: str,
    fixture_root: Path,
    fixture_digest: str,
    delivered_units: Sequence[Mapping[str, Any]],
    status: str,
    estimated_input_tokens: int | None,
    setup_ms: float,
    materialize_ms: float,
) -> dict[str, Any]:
    """Build a label-free capture record for one arm attempt."""

    log.info("capture_arm case=%s arm=%s status=%s", case_id, arm, status)
    assert_no_oracle(
        {
            "case_id": case_id,
            "arm": arm,
            "request": request,
            "edit_target": edit_target,
        }
    )
    spans = extract_delivered_spans(delivered_units)
    file_ids: list[str] = []
    symbol_ids: list[list[Any]] = []
    for unit in delivered_units:
        path = str(unit["path"])
        if path not in file_ids:
            file_ids.append(path)
        text = str(unit.get("text") or "")
        for item in symbol_ids_from_source(path, text):
            encoded = [item[0], item[1], item[2]]
            if encoded not in symbol_ids:
                symbol_ids.append(encoded)
    context_bytes = sum(int(item["byte_len"]) for item in spans)
    return {
        "case_id": case_id,
        "arm": arm,
        "request_digest": sha256_bytes(request.encode("utf-8")),
        "edit_target": edit_target,
        "fixture_root_digest": fixture_digest,
        "fixture_exists": fixture_root.is_dir(),
        "status": status,
        "spans": spans,
        "file_ids": file_ids,
        "symbol_ids": symbol_ids,
        "context_bytes": context_bytes,
        "estimated_input_tokens": estimated_input_tokens,
        "timing_ms": {
            "setup": setup_ms,
            "materialize": materialize_ms,
            "total": setup_ms + materialize_ms,
        },
    }


def deliver_units_from_installed(
    *,
    arm: str,
    fixture_root: Path,
    request: str,
    edit_target: str,
    config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, float, float]:
    """Run production V1 implement / V2 L1 packer calls in-process."""

    if not HAS_PRODUCT:
        raise RuntimeError("product_unavailable")
    setup_started = time.monotonic()
    cfg = dict(config or {})
    if arm == "v1":
        context_config = ContextConfig()
        provider = DeterministicContextProvider(fixture_root, context_config=context_config)
        state = FeatureState(
            feature_id="blind-capture",
            stage=Stage.IMPLEMENT,
            request=request,
            plan=FeaturePlan(
                goal=request,
                settings={"testing": False, "logging": "verbose", "docs": False},
                tasks=[
                    ImplementationTask(
                        id="t1",
                        title="blind",
                        description=request,
                        files=[edit_target],
                    )
                ],
            ),
            active_task_id="t1",
        )
        setup_ms = (time.monotonic() - setup_started) * 1000.0
        materialize_started = time.monotonic()
        bundle = provider.build("implement", state)
        materialize_ms = (time.monotonic() - materialize_started) * 1000.0
        units = [
            {
                "path": item.path,
                "start_line": int(item.start_line),
                "end_line": int(item.end_line),
                "text": item.text,
            }
            for item in bundle["pack"].excerpts
        ]
        return units, "ok", setup_ms, materialize_ms

    if arm == "v2":
        coverage = DependencyCoverageConfig(
            enabled=True,
            max_depth=int(cfg.get("max_depth", 2)),
            max_definitions=int(cfg.get("max_definitions", 8)),
            max_tokens=int(cfg.get("max_tokens", 1024)),
        )
        context_config = ContextConfig(
            dependency_coverage=coverage,
            packing=PackingConfig(),
        )
        app = AppContext(AppConfig(context=context_config), None)
        cache_base = Path(tempfile.mkdtemp(prefix="lw-blind-cache-"))
        index = create_code_index(app, workspace_root=fixture_root, cache_base=cache_base)
        if isinstance(index, CodeIndex):
            index.build()
        packer = create_context_packer(
            app, workspace_root=fixture_root, cache_base=cache_base
        )
        setup_ms = (time.monotonic() - setup_started) * 1000.0
        materialize_started = time.monotonic()
        result, _session = packer.open_task(
            PackRequest(
                task_id="t1",
                query=request,
                task_specification=request,
                system_instructions="Use only supplied source.",
                level="l1",
                required_paths=(edit_target,),
            )
        )
        materialize_ms = (time.monotonic() - materialize_started) * 1000.0
        pack = result.pack
        if pack is None:
            return [], "refused", setup_ms, materialize_ms
        units = [
            {
                "path": item.path,
                "start_line": int(item.start_line),
                "end_line": int(item.end_line),
                "text": item.text,
            }
            for item in pack.fragments
            if str(getattr(item, "kind", "")) != "map"
        ]
        status = "ok" if units else "refused"
        return units, status, setup_ms, materialize_ms

    raise ValueError(f"unknown_arm:{arm}")


def _invoke_worker(
    *,
    python: str,
    arm_spec: Mapping[str, Any],
    fixture_root: Path,
    request: str,
    edit_target: str,
) -> dict[str, Any]:
    stub = arm_spec.get("worker_stub")
    payload = {
        "arm": arm_spec["arm"],
        "fixture_root": str(fixture_root),
        "request": request,
        "edit_target": edit_target,
        "config": arm_spec.get("config") or {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "arm.json"
        out_path = Path(tmp) / "units.json"
        spec_path.write_text(json.dumps(payload), encoding="utf-8")
        if stub:
            argv = [python, "-I", "-B", str(stub), str(spec_path), str(out_path)]
        else:
            argv = [
                python,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "--worker",
                "--arm-spec",
                str(spec_path),
                "--worker-output",
                str(out_path),
            ]
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        )
        if completed.returncode != 0 or not out_path.is_file():
            log.error("worker_failed code=%s", completed.returncode)
            raise RuntimeError("installed_worker_failed")
        return _load_json(out_path)


def run_worker(arm_spec_path: Path, output: Path) -> dict[str, Any]:
    """Installed-wheel worker entry: product imports required unless stubbed upstream."""

    spec = _load_json(arm_spec_path)
    assert_no_oracle(spec)
    units, status, setup_ms, materialize_ms = deliver_units_from_installed(
        arm=str(spec["arm"]),
        fixture_root=Path(str(spec["fixture_root"])),
        request=str(spec["request"]),
        edit_target=str(spec["edit_target"]),
        config=spec.get("config") if isinstance(spec.get("config"), dict) else {},
    )
    payload = {
        "status": status,
        "delivered_units": units,
        "setup_ms": setup_ms,
        "materialize_ms": materialize_ms,
        "estimated_input_tokens": None,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_synthetic_capture(spec_path: Path, output: Path) -> dict[str, Any]:
    """Execute a synthetic capture spec (no product imports required)."""

    spec = _load_json(spec_path)
    assert_no_oracle(spec)
    if "oracle" in spec or "labels" in (spec.get("meta") or {}):
        raise ValueError("oracle_leak")
    started = time.monotonic()
    records = []
    for arm_spec in spec.get("arms") or []:
        units = arm_spec.get("delivered_units") or []
        record = capture_arm(
            case_id=str(spec["case_id"]),
            arm=str(arm_spec["arm"]),
            request=str(spec["request"]),
            edit_target=str(spec["edit_target"]),
            fixture_root=Path(str(spec.get("fixture_root") or ".")),
            fixture_digest=str(spec.get("fixture_digest") or "sha256:" + "0" * 64),
            delivered_units=units,
            status=str(arm_spec.get("status") or "ok"),
            estimated_input_tokens=arm_spec.get("estimated_input_tokens"),
            setup_ms=float(arm_spec.get("setup_ms") or 0.0),
            materialize_ms=float(arm_spec.get("materialize_ms") or 0.0),
        )
        records.append(record)
    payload = {
        "schema": "lw-blind-capture-v1",
        "case_id": spec["case_id"],
        "records": records,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "input_digest": sha256_file(spec_path),
        "runtime_mode": "synthetic",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("duplicate_capture")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.info("capture_done case=%s arms=%s", spec["case_id"], len(records))
    return payload


def run_installed_capture(
    spec_path: Path,
    output: Path,
    *,
    python: str,
) -> dict[str, Any]:
    """Capture from an installed interpreter via isolated worker subprocesses."""

    spec = _load_json(spec_path)
    assert_no_oracle(spec)
    fixture_root = Path(str(spec["fixture_root"])).resolve()
    relative_files = [str(item) for item in (spec.get("fixture_files") or [])]
    before = (
        fixture_inventory_digest(fixture_root, relative_files)
        if relative_files
        else str(spec.get("fixture_digest") or "")
    )
    started = time.monotonic()
    records = []
    for arm_spec in spec.get("arms") or []:
        worker = _invoke_worker(
            python=python,
            arm_spec=arm_spec,
            fixture_root=fixture_root,
            request=str(spec["request"]),
            edit_target=str(spec["edit_target"]),
        )
        record = capture_arm(
            case_id=str(spec["case_id"]),
            arm=str(arm_spec["arm"]),
            request=str(spec["request"]),
            edit_target=str(spec["edit_target"]),
            fixture_root=fixture_root,
            fixture_digest=before or str(spec.get("fixture_digest") or ""),
            delivered_units=worker.get("delivered_units") or [],
            status=str(worker.get("status") or "ok"),
            estimated_input_tokens=worker.get("estimated_input_tokens"),
            setup_ms=float(worker.get("setup_ms") or 0.0),
            materialize_ms=float(worker.get("materialize_ms") or 0.0),
        )
        records.append(record)
    after = (
        fixture_inventory_digest(fixture_root, relative_files)
        if relative_files
        else before
    )
    if relative_files and before != after:
        raise RuntimeError("fixture_changed")
    payload = {
        "schema": "lw-blind-capture-v1",
        "case_id": spec["case_id"],
        "records": records,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "input_digest": sha256_file(spec_path),
        "fixture_digest_before": before,
        "fixture_digest_after": after,
        "runtime_mode": "installed-wheel",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("duplicate_capture")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.info("installed_capture_done case=%s arms=%s", spec["case_id"], len(records))
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="release_blind_capture")
    parser.add_argument("--spec", default=None, help="Label-free capture specification JSON")
    parser.add_argument("--output", default=None)
    parser.add_argument("--mode", choices=("synthetic", "installed"), default="synthetic")
    parser.add_argument("--python", default=None, help="Installed interpreter for --mode installed")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--arm-spec", default=None)
    parser.add_argument("--worker-output", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.worker:
            if not args.arm_spec or not args.worker_output:
                raise ValueError("worker_args_missing")
            run_worker(Path(args.arm_spec).resolve(), Path(args.worker_output).resolve())
            print(json.dumps({"ok": True, "mode": "worker"}, sort_keys=True))
            return 0
        if not args.spec or not args.output:
            raise ValueError("spec_and_output_required")
        if args.mode == "synthetic":
            run_synthetic_capture(Path(args.spec).resolve(), Path(args.output).resolve())
        else:
            if not args.python:
                raise ValueError("python_required_for_installed_mode")
            run_installed_capture(
                Path(args.spec).resolve(),
                Path(args.output).resolve(),
                python=str(args.python),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("capture_failed reason=%s", exc)
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
