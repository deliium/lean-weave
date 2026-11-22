"""Materialize pinned benchmark fixture repositories for offline MVP validation.

Fixtures live under ``benchmarks/fixtures/``. Oracles and intended edits stay
outside the materialized Git tree so they never enter model context.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("lean_weave.benchmarks.fixtures")

SCHEMA_VERSION = 1
FIXED_AUTHOR_NAME = "Fixture"
FIXED_AUTHOR_EMAIL = "fixture@example.invalid"
FIXED_DATE = "2026-01-01T00:00:00Z"
COMMIT_MESSAGE = "fixture baseline"

CASE_IDS: tuple[str, ...] = (
    "bug_fix",
    "single_file_feature",
    "multi_file_feature",
    "test_repair",
    "verify_then_repair",
    "insufficient_context",
    "budget_exhaustion",
)


class FixtureError(RuntimeError):
    """Raised when a fixture manifest or materialization is invalid."""


@dataclass(frozen=True)
class CheckSpec:
    id: str
    oracle: str
    argv_template: tuple[str, ...]
    target: str | None
    timeout_s: float
    output_bytes_max: int
    runs_workspace_code: bool


@dataclass(frozen=True)
class FixtureManifest:
    id: str
    request: str
    tree: str
    expected_head: str
    write_paths: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    check_ids: tuple[str, ...]
    checks: tuple[CheckSpec, ...]
    expected_behavior: Mapping[str, Any]
    pass_criteria: Mapping[str, Any]
    workflow_limits: Mapping[str, Any]
    task_plan: tuple[Mapping[str, Any], ...]

    @property
    def positive_outcome(self) -> bool:
        return str(self.expected_behavior.get("outcome", "")) == "success"


@dataclass(frozen=True)
class MaterializedFixture:
    manifest: FixtureManifest
    root: Path
    head: str


@dataclass(frozen=True)
class OracleResult:
    check_id: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def package_root() -> Path:
    return Path(__file__).resolve().parent


def fixtures_data_root() -> Path:
    return package_root() / "fixtures"


def manifests_dir() -> Path:
    return fixtures_data_root() / "manifests"


def trees_dir() -> Path:
    return fixtures_data_root() / "trees"


def oracles_dir() -> Path:
    return fixtures_data_root() / "oracles"


def edits_dir() -> Path:
    return fixtures_data_root() / "edits"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"unreadable_manifest:{path.name}") from exc
    if not isinstance(payload, dict):
        raise FixtureError(f"invalid_manifest_type:{path.name}")
    return payload


def _require_str(payload: Mapping[str, Any], key: str, *, case_id: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"missing_field:{case_id}:{key}")
    return value.strip()


def _require_str_list(payload: Mapping[str, Any], key: str, *, case_id: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise FixtureError(f"missing_field:{case_id}:{key}")
    return tuple(item.strip() for item in value)


def load_manifest(case_id: str) -> FixtureManifest:
    path = manifests_dir() / f"{case_id}.json"
    if not path.is_file():
        raise FixtureError(f"unknown_case:{case_id}")
    payload = _read_json(path)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError(f"unsupported_schema:{case_id}")
    if _require_str(payload, "id", case_id=case_id) != case_id:
        raise FixtureError(f"id_mismatch:{case_id}")
    checks_raw = payload.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise FixtureError(f"missing_field:{case_id}:checks")
    checks: list[CheckSpec] = []
    for item in checks_raw:
        if not isinstance(item, dict):
            raise FixtureError(f"invalid_check:{case_id}")
        check_id = _require_str(item, "id", case_id=case_id)
        oracle = _require_str(item, "oracle", case_id=case_id)
        argv = item.get("argv_template")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(entry, str) and entry for entry in argv
        ):
            raise FixtureError(f"invalid_argv:{case_id}:{check_id}")
        target = item.get("target")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise FixtureError(f"invalid_target:{case_id}:{check_id}")
        checks.append(
            CheckSpec(
                id=check_id,
                oracle=oracle,
                argv_template=tuple(argv),
                target=target.strip() if isinstance(target, str) else None,
                timeout_s=float(item.get("timeout_s", 5)),
                output_bytes_max=int(item.get("output_bytes_max", 65_536)),
                runs_workspace_code=bool(item.get("runs_workspace_code", False)),
            )
        )
    check_ids = _require_str_list(payload, "check_ids", case_id=case_id)
    if tuple(check.id for check in checks) != check_ids:
        raise FixtureError(f"check_id_mismatch:{case_id}")
    behavior = payload.get("expected_behavior")
    criteria = payload.get("pass_criteria")
    limits = payload.get("workflow_limits") or {}
    plan = payload.get("task_plan") or []
    if not isinstance(behavior, dict) or not isinstance(criteria, dict):
        raise FixtureError(f"missing_field:{case_id}:expected")
    if not isinstance(limits, dict) or not isinstance(plan, list):
        raise FixtureError(f"missing_field:{case_id}:plan_or_limits")
    for entry in plan:
        if not isinstance(entry, dict):
            raise FixtureError(f"invalid_task_plan:{case_id}")
    manifest = FixtureManifest(
        id=case_id,
        request=_require_str(payload, "request", case_id=case_id),
        tree=_require_str(payload, "tree", case_id=case_id),
        expected_head=_require_str(payload, "expected_head", case_id=case_id).lower(),
        write_paths=_require_str_list(payload, "write_paths", case_id=case_id),
        allowed_operations=_require_str_list(
            payload, "allowed_operations", case_id=case_id
        ),
        check_ids=check_ids,
        checks=tuple(checks),
        expected_behavior=behavior,
        pass_criteria=criteria,
        workflow_limits=limits,
        task_plan=tuple(plan),
    )
    log.debug(
        "[Fixture.load] id=%s head=%s checks=%s",
        manifest.id,
        manifest.expected_head[:12],
        ",".join(manifest.check_ids),
    )
    return manifest


def list_manifests() -> tuple[FixtureManifest, ...]:
    return tuple(load_manifest(case_id) for case_id in CASE_IDS)


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": FIXED_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": FIXED_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": FIXED_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": FIXED_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": FIXED_DATE,
        "GIT_COMMITTER_DATE": FIXED_DATE,
        "LANG": "C",
        "LC_ALL": "C",
    }


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    home = root / ".fixture-home"
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(home),
    )


_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        ".fixture-home",
    }
)
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _normalize_mode(path: Path) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
    if path.suffix == ".py" and "oracles" in path.parts:
        mode |= stat.S_IXUSR
    path.chmod(mode)


def _is_cache_noise(relative: Path) -> bool:
    """True for bytecode/cache paths that must never enter fixture commits."""

    if any(part in _SKIP_DIR_NAMES for part in relative.parts):
        return True
    return relative.suffix in _SKIP_SUFFIXES


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FixtureError(f"destination_exists:{destination}")
    destination.mkdir(parents=True, exist_ok=False)
    skipped = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _is_cache_noise(relative):
            skipped += 1
            log.debug(
                "[FIX] skip_cache_entry path=%s",
                relative.as_posix(),
            )
            continue
        target = destination / relative
        if path.is_symlink():
            raise FixtureError(f"symlink_forbidden:{relative.as_posix()}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise FixtureError(f"unsupported_entry:{relative.as_posix()}")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        _normalize_mode(target)
    if skipped:
        log.info("[FIX] skipped_cache_entries count=%s source=%s", skipped, source.name)


def materialize(case_id: str, destination: Path) -> MaterializedFixture:
    """Create a fresh temporary Git repo for ``case_id`` under ``destination``."""

    log.debug("[Fixture.materialize] start id=%s dest=%s", case_id, destination)
    manifest = load_manifest(case_id)
    source = trees_dir() / manifest.tree
    if not source.is_dir():
        raise FixtureError(f"missing_tree:{manifest.tree}")
    if destination.exists():
        raise FixtureError(f"destination_exists:{destination}")
    _copy_tree(source, destination)
    _run_git(destination, "init", "-q", "-b", "main")
    _run_git(destination, "config", "user.email", FIXED_AUTHOR_EMAIL)
    _run_git(destination, "config", "user.name", FIXED_AUTHOR_NAME)
    _run_git(destination, "config", "core.autocrlf", "false")
    _run_git(destination, "config", "core.filemode", "true")
    tracked = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    )
    if not tracked:
        raise FixtureError(f"empty_tree:{case_id}")
    _run_git(destination, "add", "--", *tracked)
    _run_git(destination, "commit", "-qm", COMMIT_MESSAGE)
    head = _run_git(destination, "rev-parse", "HEAD").stdout.decode("ascii").strip().lower()
    if head != manifest.expected_head:
        log.error(
            "[Fixture.materialize] mismatch id=%s expected=%s actual=%s",
            case_id,
            manifest.expected_head,
            head,
        )
        raise FixtureError(
            f"head_mismatch:{case_id}:expected={manifest.expected_head}:actual={head}"
        )
    status = _run_git(destination, "status", "--porcelain=v1", "-uall").stdout
    if status:
        raise FixtureError(f"dirty_after_commit:{case_id}")
    remotes = _run_git(destination, "remote").stdout.strip()
    if remotes:
        raise FixtureError(f"unexpected_remote:{case_id}")
    log.debug(
        "[Fixture.materialize] ok id=%s head=%s files=%s",
        case_id,
        head[:12],
        len(tracked),
    )
    return MaterializedFixture(manifest=manifest, root=destination, head=head)


def intended_edit_files(case_id: str) -> dict[str, str]:
    path = edits_dir() / f"{case_id}.json"
    payload = _read_json(path)
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise FixtureError(f"missing_edit:{case_id}")
    result: dict[str, str] = {}
    for key, value in files.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise FixtureError(f"invalid_edit:{case_id}")
        result[key] = value.replace("\r\n", "\n")
    return result


def apply_intended_edit(case_id: str, workspace: Path) -> None:
    for relative, content in intended_edit_files(case_id).items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        _normalize_mode(path)


def resolve_check_argv(
    check: CheckSpec,
    *,
    python: str | None = None,
) -> list[str]:
    """Resolve trusted argv.

    Workspace file arguments stay project-relative so ``bind_local`` accepts
    them (absolute paths under the approval root are rejected). The oracle
    executable stays absolute and outside the workspace.
    """

    oracle_path = oracles_dir() / check.oracle
    if not oracle_path.is_file():
        raise FixtureError(f"missing_oracle:{check.oracle}")
    mapping = {
        "{python}": python or sys.executable,
        "{oracle}": str(oracle_path.resolve()),
        "{workspace}": ".",
        "{target}": check.target if check.target is not None else ".",
    }
    resolved: list[str] = []
    for entry in check.argv_template:
        value = entry
        for key, replacement in mapping.items():
            value = value.replace(key, replacement)
        resolved.append(value)
    return resolved


def run_oracle(
    manifest: FixtureManifest,
    workspace: Path,
    check_id: str,
    *,
    python: str | None = None,
) -> OracleResult:
    check = next((item for item in manifest.checks if item.id == check_id), None)
    if check is None:
        raise FixtureError(f"unknown_check:{manifest.id}:{check_id}")
    argv = resolve_check_argv(check, python=python)
    log.debug(
        "[Fixture.oracle] id=%s check=%s argv0=%s",
        manifest.id,
        check_id,
        Path(argv[0]).name if argv else "",
    )
    completed = subprocess.run(
        argv,
        check=False,
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workspace),
            "LANG": "C",
            "LC_ALL": "C",
        },
        timeout=check.timeout_s,
    )
    return OracleResult(
        check_id=check_id,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_all_oracles(
    manifest: FixtureManifest,
    workspace: Path,
    *,
    python: str | None = None,
) -> tuple[OracleResult, ...]:
    return tuple(
        run_oracle(manifest, workspace, check_id, python=python)
        for check_id in manifest.check_ids
    )


def weakened_test_variants() -> dict[str, str]:
    """Negative variants the test-repair oracle must reject."""

    return {
        "deleted": "",
        "pass_only": "from src.mathops import add\n\n\ndef test_add():\n    pass\n",
        "wrong_value": (
            "from src.mathops import add\n\n\ndef test_add():\n    assert add(1, 1) == 0\n"
        ),
    }


def compute_head(case_id: str, scratch: Path) -> str:
    """Materialize without enforcing ``expected_head`` (used to pin manifests)."""

    destination = scratch / case_id
    if destination.exists():
        shutil.rmtree(destination)
    payload = _read_json(manifests_dir() / f"{case_id}.json")
    tree = payload["tree"]
    source = trees_dir() / tree
    _copy_tree(source, destination)
    _run_git(destination, "init", "-q", "-b", "main")
    _run_git(destination, "config", "user.email", FIXED_AUTHOR_EMAIL)
    _run_git(destination, "config", "user.name", FIXED_AUTHOR_NAME)
    _run_git(destination, "config", "core.autocrlf", "false")
    _run_git(destination, "config", "core.filemode", "true")
    tracked = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".fixture-home" not in path.parts
    )
    _run_git(destination, "add", "--", *tracked)
    _run_git(destination, "commit", "-qm", COMMIT_MESSAGE)
    return _run_git(destination, "rev-parse", "HEAD").stdout.decode("ascii").strip().lower()

