"""Stable execution-condition capture for reproducible benchmark evidence.

Developer tooling only. Every value here is a bounded digest, count, version or
normalized identifier. Raw config bodies, prompts, source text and absolute
developer paths never enter a persisted result.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lean_weave.benchmark.fixtures import FixtureManifest, resolve_check_argv
from lean_weave.config import load_config
from lean_weave.errors import ConfigError

SOURCE_ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_LOCK_NAMES = ("uv.lock", "poetry.lock", "requirements.lock", "Pipfile.lock")
SOURCE_PATHS = ("src", "benchmarks", "pyproject.toml", "uv.lock")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_digest(payload: Mapping[str, Any] | list[Any] | str) -> str:
    """Digest a JSON-shaped value with deterministic key ordering."""

    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    return _sha256(encoded)


def _git_output(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(SOURCE_ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _git_bytes(*args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(SOURCE_ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return b""
    return completed.stdout


def source_revision() -> str:
    """Best-effort verified package source identity for provenance only."""

    return _git_output("rev-parse", "HEAD") or "unknown"


def source_tree_digest() -> str | None:
    """Digest the checked-out code/build tree, including uncommitted changes.

    Documentation and plan edits do not change executable identity, so the
    digest covers ``src``, ``benchmarks``, ``tests``, ``pyproject.toml`` and
    the dependency lock. Release evidence built from a dirty tree cannot be
    mistaken for the clean commit identity alone.
    """

    head = _git_output("rev-parse", "HEAD")
    if head is None:
        return None
    pathspec = list(SOURCE_PATHS)
    diff = _git_bytes("diff", "HEAD", "--binary", "--", *pathspec)
    untracked = _git_bytes(
        "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec
    )
    return _sha256(head.encode("ascii") + b"\0" + diff + b"\0" + untracked)


def code_tree_dirty() -> bool:
    status = _git_bytes(
        "status", "--porcelain=v1", "-uall", "--", *SOURCE_PATHS
    )
    return bool(status.strip())


def installation_kind() -> str:
    package = Path(__file__).resolve()
    try:
        package.relative_to(SOURCE_ROOT)
    except ValueError:
        return "installed"
    return "source"


def wheel_sha256() -> str | None:
    """Hash an explicitly supplied wheel artifact without persisting its path."""

    candidate = os.environ.get("LEAN_WEAVE_BENCHMARK_WHEEL", "").strip()
    if not candidate:
        return None
    path = Path(candidate)
    if not path.is_file() or path.suffix != ".whl":
        return None
    return _sha256(path.read_bytes())


def release_binding() -> dict[str, Any]:
    """Describe whether this run can qualify as formal release evidence.

    Observational fields remain compatible with historical records. An
    environment-supplied wheel hash alone is never formal proof: new runs
    require the verified artifact-binding protocol under
    ``benchmarks/release_evidence.py``. Dirty trees and missing hashes remain
    explicit ineligibility reasons.
    """

    dirty = code_tree_dirty()
    kind = installation_kind()
    wheel = wheel_sha256()
    reasons: list[str] = []
    if dirty:
        reasons.append("source_tree_dirty")
    if wheel is None:
        reasons.append("wheel_hash_unavailable")
        if kind == "source":
            reasons.append("source_run_no_wheel")
    else:
        # LEAN_WEAVE_BENCHMARK_WHEEL is observational only; it cannot certify.
        reasons.append("formal_binding_required")
    return {
        "available": True,
        "installation_kind": kind,
        "source_revision": source_revision(),
        "source_tree_digest": source_tree_digest(),
        "dependency_lock_digest": dependency_lock_digest(),
        "wheel_sha256": wheel,
        "release_eligible": not reasons,
        "reasons": reasons,
    }


def dependency_lock_digest() -> str | None:
    for name in DEPENDENCY_LOCK_NAMES:
        candidate = SOURCE_ROOT / name
        if candidate.is_file():
            return _sha256(candidate.read_bytes())
    return None


def runtime_conditions() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_impl": platform.python_implementation(),
        "git": _git_output("--version") or "unavailable",
        "platform": platform.system().lower() or "unknown",
    }


def fixture_conditions(manifest: FixtureManifest, *, origin_head: str | None) -> dict[str, Any]:
    """Fixture identity: pinned HEAD plus canonical manifest content."""

    return {
        "case_id": manifest.id,
        "pinned_head": manifest.expected_head,
        "origin_head": (origin_head or manifest.expected_head).lower(),
        "manifest_digest": canonical_digest(
            {
                "request": manifest.request,
                "tree": manifest.tree,
                "write_paths": list(manifest.write_paths),
                "allowed_operations": list(manifest.allowed_operations),
                "check_ids": list(manifest.check_ids),
                "expected_behavior": dict(manifest.expected_behavior),
                "pass_criteria": dict(manifest.pass_criteria),
                "task_plan": [dict(item) for item in manifest.task_plan],
            }
        ),
    }


def verifier_conditions(manifest: FixtureManifest) -> dict[str, Any]:
    """Normalized verifier identity without fixture-specific path noise."""

    checks: list[dict[str, Any]] = []
    for check in manifest.checks:
        argv = resolve_check_argv(check)
        oracle = Path(argv[0]) if argv else None
        oracle_digest = None
        if oracle is not None and oracle.is_file() and argv[1:2]:
            candidate = Path(argv[1])
            if candidate.is_file():
                oracle_digest = _sha256(candidate.read_bytes())
        normalized = {
            "id": check.id,
            "executable": Path(argv[0]).name if argv else "",
            "argv": _stable_argv(argv),
            "argv_digest": canonical_digest(_stable_argv(argv)),
            "timeout_s": check.timeout_s,
            "output_bytes_max": check.output_bytes_max,
            "runs_workspace_code": check.runs_workspace_code,
            "oracle_digest": oracle_digest,
        }
        checks.append(normalized)
    return {
        "checks": checks,
        "digest": canonical_digest(
            [
                {
                    key: value
                    for key, value in item.items()
                    if key in {"id", "argv", "timeout_s", "output_bytes_max", "runs_workspace_code"}
                }
                for item in checks
            ]
        ),
    }


def _stable_argv(argv: list[str]) -> list[str]:
    """Replace machine-specific absolute prefixes with stable tokens."""

    stable: list[str] = []
    for entry in argv:
        renamed = entry
        if renamed.startswith(str(SOURCE_ROOT)):
            renamed = "${source}" + renamed[len(str(SOURCE_ROOT)) :]
        elif renamed.startswith(sys.prefix):
            renamed = "${python_prefix}" + renamed[len(sys.prefix) :]
        stable.append(renamed)
    return stable


def config_conditions(config_path: Path | None) -> dict[str, Any] | None:
    """Effective operator settings as digests and declared caps only."""

    if config_path is None or not config_path.is_file():
        return {"available": False, "gap": "config_missing"}
    try:
        loaded = load_config(config_path, required=True).config
    except ConfigError:
        return {"available": False, "gap": "config_invalid"}
    context = loaded.context.model_dump(mode="json") if loaded.context else {}
    return {
        "available": True,
        "digest": canonical_digest(loaded.model_dump(mode="json")),
        "model": loaded.llm.model,
        "thinking": loaded.llm.thinking,
        "base_url": loaded.llm.base_url,
        "max_output_tokens": loaded.llm.max_output_tokens,
        "gateway_max_retries": loaded.llm.max_retries,
        "workflow": dict(loaded.workflow.model_dump(mode="json")),
        "workflow_digest": canonical_digest(loaded.workflow.model_dump(mode="json")),
        "write_allow": sorted(loaded.tools.write_allow),
        "write_allow_digest": canonical_digest(sorted(loaded.tools.write_allow)),
        "check_count": len(loaded.tools.checks),
        "context": context,
        "context_digest": canonical_digest(context),
    }


def capture_arm_conditions(
    *,
    manifest: FixtureManifest,
    origin_head: str | None,
    variant_name: str,
    context_mode: str,
    context_extra: Mapping[str, Any] | None,
    config_path: Path | None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Capture the effective contract for one arm before dispatch."""

    payload: dict[str, Any] = {
        "variant": variant_name,
        "mode": mode,
        "context_mode": context_mode,
        "context_extra": dict(context_extra or {}),
        "request_digest": canonical_digest(manifest.request),
        "expected_contract": dict(manifest.expected_behavior),
        "fixture": fixture_conditions(manifest, origin_head=origin_head),
        "verifier": verifier_conditions(manifest),
        "approval_write_paths": list(manifest.write_paths),
        "config": config_conditions(config_path),
        "runtime": runtime_conditions(),
        "dependency_lock_digest": dependency_lock_digest(),
        "source_tree_digest": source_tree_digest(),
        "release_binding": release_binding(),
    }
    payload["digest"] = canonical_digest(payload)
    return payload


def scenario_definition(
    *,
    case_id: str,
    variant: str,
    repetition: int,
    manifest: FixtureManifest,
    context_mode: str,
    context_extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Ordered, persisted schedule entry independent of row insertion order."""

    return {
        "suite": "workflow",
        "case_id": case_id,
        "variant": variant,
        "repetition": repetition,
        "context_mode": context_mode,
        "context_extra": dict(context_extra or {}),
        "request_digest": canonical_digest(manifest.request),
        "fixture_head": manifest.expected_head,
        "scenario_digest": canonical_digest(
            {
                "case_id": case_id,
                "variant": variant,
                "repetition": repetition,
                "context_mode": context_mode,
                "context_extra": dict(context_extra or {}),
                "request": manifest.request,
                "expected": dict(manifest.expected_behavior),
            }
        ),
    }
