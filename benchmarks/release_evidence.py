#!/usr/bin/env python3
"""Developer-only V2 release evidence driver.

Stdlib-only so an operator can extract this file as a committed blob and launch
it with ``<pinned-python> -I -B <external>/release_evidence.py``. Never put the
authoring tree on the installed worker's module search path. Product CLI and
wheel packaging defaults are unchanged.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_BUILD_PROOF = "lw-build-proof-v1"
SCHEMA_ARTIFACT_BINDING = "lw-artifact-binding-v1"
SCHEMA_RELEASE_RUN = "lw-release-run-v1"
SCHEMA_EVIDENCE_ENVELOPE = "lw-evidence-envelope-v1"
SCHEMA_OFFLINE_RECEIPT = "lw-offline-receipt-v1"
SCHEMA_LIVE_RECEIPT = "lw-live-receipt-v1"
SCHEMA_RELEASE_DECISION = "lw-release-decision-v1"

DRIVER_RELATIVE = "benchmarks/release_evidence.py"
FULL_OID_RE = re.compile(r"^[0-9a-f]{40}$")

REASON_COMMIT_INVALID = "commit_invalid"
REASON_STATUS_FAILED = "status_failed"
REASON_SOURCE_TREE_DIRTY = "source_tree_dirty"
REASON_BUILD_FAILED = "build_failed"
REASON_WHEEL_MISMATCH = "wheel_mismatch"
REASON_BINDING_INCOMPLETE = "binding_incomplete"
REASON_BINDING_MISMATCH = "binding_mismatch"
REASON_INSTALLED_UNVERIFIED = "installed_wheel_unverified"
REASON_OFFLINE_FAILED = "offline_failed"
REASON_REQUIRED_NODE_MISSING = "required_node_missing"
REASON_SCHEDULE_INCOMPLETE = "schedule_incomplete"
REASON_ARTIFACT_CHANGED = "artifact_changed"
REASON_HOLDOUT_MISSING = "holdout_missing"
REASON_HOLDOUT_INVALID = "holdout_invalid"

log = logging.getLogger("lean_weave.release_evidence")


def _configure_logging() -> None:
    selected = (
        os.getenv("LEAN_WEAVE_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    ).upper()
    level = getattr(logging, selected, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_digest(payload: Mapping[str, Any] | list[Any] | str) -> str:
    """Match ``lean_weave.benchmark.conditions.canonical_digest`` exactly."""

    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    return sha256_bytes(encoded)


def _redact_path(path: Path | str) -> str:
    name = Path(path).name
    return f"<path>/{name}"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("json_root_not_object")
    return raw


def _git(
    repo: Path,
    *args: str,
    git_exe: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cleaned = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"HOME", "XDG_CONFIG_HOME"}
    }
    if env:
        cleaned.update(env)
    cleaned["GIT_CONFIG_GLOBAL"] = "/dev/null"
    cleaned["GIT_CONFIG_SYSTEM"] = "/dev/null"
    cleaned["GIT_TERMINAL_PROMPT"] = "0"
    cleaned["GIT_OPTIONAL_LOCKS"] = "0"
    log.debug(
        "git_exec stage=%s args=%s",
        args[0] if args else "?",
        list(args[:6]),
    )
    return subprocess.run(
        [git_exe, "-C", str(repo), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=cleaned,
    )


def _require_full_commit(repo: Path, commit: str, git_exe: str) -> str:
    if not FULL_OID_RE.fullmatch(commit):
        raise ValueError(REASON_COMMIT_INVALID)
    try:
        resolved = _git(
            repo, "rev-parse", "--verify", f"{commit}^{{commit}}", git_exe=git_exe
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(REASON_COMMIT_INVALID) from exc
    if resolved != commit:
        raise ValueError(REASON_COMMIT_INVALID)
    # Reject tags / annotated objects that happen to resolve to the same OID.
    kind = _git(repo, "cat-file", "-t", commit, git_exe=git_exe).stdout.strip()
    if kind != "commit":
        raise ValueError(REASON_COMMIT_INVALID)
    return commit


def _commit_tree(repo: Path, commit: str, git_exe: str) -> str:
    return _git(repo, "rev-parse", f"{commit}^{{tree}}", git_exe=git_exe).stdout.strip()


def _status_z(repo: Path, git_exe: str) -> bytes:
    completed = subprocess.run(
        [
            git_exe,
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(REASON_STATUS_FAILED)
    return completed.stdout


def _observe_clean(repo: Path, git_exe: str) -> dict[str, Any]:
    try:
        status = _status_z(repo, git_exe)
    except RuntimeError:
        return {"ok": False, "clean": False, "reason": REASON_STATUS_FAILED}
    clean = status == b""
    return {
        "ok": True,
        "clean": clean,
        "reason": None if clean else REASON_SOURCE_TREE_DIRTY,
        "status_digest": sha256_bytes(status),
    }


def _verify_tracked_bytes(repo: Path, commit: str, git_exe: str) -> None:
    """Compare worktree tracked bytes against ls-tree/raw blobs."""

    listing = _git(
        repo, "ls-tree", "-r", "-z", commit, git_exe=git_exe
    ).stdout.encode("utf-8")
    entries = [item for item in listing.split(b"\0") if item]
    for entry in entries:
        # mode SP type SP sha TAB path
        meta, path_bytes = entry.split(b"\t", 1)
        parts = meta.split(b" ")
        if len(parts) != 3:
            raise RuntimeError("tree_entry_invalid")
        mode, kind, blob = parts
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        if kind != b"blob":
            raise RuntimeError("unsupported_tree_entry")
        if mode not in {b"100644", b"100755"}:
            raise RuntimeError("unsupported_tree_mode")
        target = repo / path
        if not target.is_file() or target.is_symlink():
            raise RuntimeError("tracked_path_missing_or_link")
        blob_bytes = subprocess.run(
            [git_exe, "-C", str(repo), "cat-file", "blob", blob.decode("ascii")],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        actual = target.read_bytes()
        if actual != blob_bytes:
            raise RuntimeError("tracked_bytes_mismatch")
        executable = bool(target.stat().st_mode & stat.S_IXUSR)
        if mode == b"100755" and not executable:
            raise RuntimeError("tracked_mode_mismatch")
        if mode == b"100644" and executable:
            raise RuntimeError("tracked_mode_mismatch")


def _driver_blob_id(repo: Path, commit: str, git_exe: str) -> tuple[str, str]:
    listing = _git(
        repo, "ls-tree", "-r", commit, "--", DRIVER_RELATIVE, git_exe=git_exe
    ).stdout.strip()
    if not listing:
        raise RuntimeError("driver_missing_from_commit")
    # mode type sha\tpath
    meta = listing.split("\t", 1)[0].split()
    blob_id = meta[2]
    digest = sha256_bytes(
        subprocess.run(
            [git_exe, "-C", str(repo), "cat-file", "blob", blob_id],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    )
    return blob_id, digest


def _self_check(repo: Path, commit: str, git_exe: str) -> dict[str, Any]:
    """Fail closed when the running driver bytes differ from the committed blob."""

    blob_id, digest = _driver_blob_id(repo, commit, git_exe)
    here = Path(__file__).resolve()
    if here.is_file():
        local = sha256_file(here)
        if local != digest:
            log.error(
                "driver_bytes_mismatch blob=%s",
                blob_id[:12],
            )
            raise RuntimeError("driver_bytes_mismatch")
    return {"blob_id": blob_id, "digest": digest, "relative": DRIVER_RELATIVE}


def _commit_source_date_epoch(repo: Path, commit: str, git_exe: str) -> str:
    """Return SOURCE_DATE_EPOCH seconds derived from the commit author time."""

    value = _git(
        repo, "show", "-s", "--format=%ct", commit, git_exe=git_exe
    ).stdout.strip()
    if not value.isdigit():
        raise RuntimeError("commit_time_invalid")
    return value


def _load_build_inputs(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    required = (
        "python",
        "git",
        "uv",
        "mode",
    )
    for key in required:
        if key not in data:
            raise ValueError(f"build_inputs_missing:{key}")
    return data


def _new_attempt_dir(output: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    attempt = output / f"build-{stamp}-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(parents=True, exist_ok=False)
    return attempt


def _write_synthetic_wheel(directory: Path, filename: str, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(payload)
    return path


def _run_real_uv_build(
    *,
    worktree: Path,
    out_dir: Path,
    inputs: Mapping[str, Any],
    source_date_epoch: str,
) -> tuple[int, Path | None, str | None]:
    python = str(inputs["python"])
    uv = str(inputs["uv"])
    wheelhouse = Path(str(inputs["wheelhouse"]))
    constraints = Path(str(inputs["build_constraints"]))
    expected_name = inputs.get("wheel_filename")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UV_")
    }
    env.update(
        {
            "HOME": str(inputs["home"]),
            "TMPDIR": str(inputs["tmpdir"]),
            "XDG_CACHE_HOME": str(inputs["cache"]),
            "SOURCE_DATE_EPOCH": source_date_epoch,
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    argv = [
        uv,
        "--no-config",
        "--offline",
        "build",
        "--force-pep517",
        "--wheel",
        "--python",
        python,
        "--no-python-downloads",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--build-constraints",
        str(constraints),
        "--require-build-hashes",
        "--out-dir",
        str(out_dir),
        str(worktree),
    ]
    log.debug(
        "uv_build argv_digest=%s source_date_epoch=%s",
        canonical_digest(argv),
        source_date_epoch,
    )
    completed = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(inputs["external_cwd"]),
    )
    if completed.returncode != 0:
        log.error("build_failed code=%s", completed.returncode)
        return completed.returncode, None, REASON_BUILD_FAILED
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        return completed.returncode, None, REASON_BUILD_FAILED
    if expected_name and wheels[0].name != str(expected_name):
        return completed.returncode, None, REASON_BUILD_FAILED
    return 0, wheels[0], None


def _add_worktree(
    repo: Path,
    commit: str,
    target: Path,
    git_exe: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(
        repo,
        "worktree",
        "add",
        "--detach",
        str(target),
        commit,
        git_exe=git_exe,
    )


def _remove_worktree(repo: Path, target: Path, git_exe: str) -> None:
    if not target.exists():
        return
    try:
        _git(repo, "worktree", "remove", str(target), git_exe=git_exe)
        return
    except subprocess.CalledProcessError:
        log.debug("worktree_remove_retry path=%s", _redact_path(target))
    try:
        _git(repo, "worktree", "remove", "--force", str(target), git_exe=git_exe)
    except subprocess.CalledProcessError:
        shutil.rmtree(target, ignore_errors=True)
        try:
            _git(repo, "worktree", "prune", git_exe=git_exe, check=False)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log.debug("worktree_prune_failed path=%s", _redact_path(target))


def cmd_build(args: argparse.Namespace) -> int:
    started = time.monotonic()
    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    inputs_path = Path(args.build_inputs).resolve()
    attempt = _new_attempt_dir(output)
    log.info("build_start commit=%s attempt=%s", args.commit[:12], attempt.name)

    errors: list[str] = []
    proof: dict[str, Any] = {
        "schema": SCHEMA_BUILD_PROOF,
        "commit": None,
        "tree": None,
        "driver": None,
        "builds": [],
        "matching": False,
        "errors": errors,
        "tool": {},
        "inputs_digest": None,
    }
    try:
        inputs = _load_build_inputs(inputs_path)
        git_exe = str(inputs["git"])
        commit = _require_full_commit(repo, args.commit, git_exe)
        tree = _commit_tree(repo, commit, git_exe)
        proof["commit"] = commit
        proof["tree"] = tree
        proof["driver"] = _self_check(repo, commit, git_exe)
        proof["inputs_digest"] = sha256_file(inputs_path)
        proof["tool"] = {
            "python": str(inputs["python"]),
            "git": git_exe,
            "uv": str(inputs.get("uv", "")),
            "mode": str(inputs["mode"]),
        }
        mode = str(inputs["mode"])
        expected_name = str(inputs.get("wheel_filename", "lean_weave-0.1.0-py3-none-any.whl"))
        synthetic_payload = inputs.get("synthetic_wheel_bytes")
        if "source_date_epoch" in inputs and inputs["source_date_epoch"] is not None:
            source_date_epoch = str(inputs["source_date_epoch"])
        else:
            source_date_epoch = _commit_source_date_epoch(repo, commit, git_exe)
        proof["tool"]["source_date_epoch"] = source_date_epoch
        digests: list[str] = []
        names: list[str] = []

        for index in (1, 2):
            build_root = attempt / f"worktree-{index}"
            wheel_dir = attempt / f"wheels-{index}"
            wheel_dir.mkdir(parents=True, exist_ok=True)
            build_record: dict[str, Any] = {
                "index": index,
                "before": None,
                "after": None,
                "exit_status": None,
                "wheel_filename": None,
                "wheel_sha256": None,
                "preserved_relative": None,
                "reason": None,
            }
            try:
                _add_worktree(repo, commit, build_root, git_exe)
                before = _observe_clean(build_root, git_exe)
                build_record["before"] = before
                if not before["ok"] or not before["clean"]:
                    reason = before.get("reason") or REASON_SOURCE_TREE_DIRTY
                    build_record["reason"] = reason
                    errors.append(reason)
                    proof["builds"].append(build_record)
                    continue
                if mode != "synthetic":
                    _verify_tracked_bytes(build_root, commit, git_exe)
                if mode == "synthetic":
                    if not isinstance(synthetic_payload, str):
                        raise RuntimeError("synthetic_payload_missing")
                    payload = bytes.fromhex(synthetic_payload)
                    wheel = _write_synthetic_wheel(wheel_dir, expected_name, payload)
                    build_record["exit_status"] = 0
                else:
                    code, wheel, reason = _run_real_uv_build(
                        worktree=build_root,
                        out_dir=wheel_dir,
                        inputs={
                            **inputs,
                            "home": str(attempt / f"home-{index}"),
                            "tmpdir": str(attempt / f"tmp-{index}"),
                            "cache": str(attempt / f"cache-{index}"),
                            "external_cwd": str(attempt / "cwd"),
                        },
                        source_date_epoch=source_date_epoch,
                    )
                    build_record["exit_status"] = code
                    if reason:
                        build_record["reason"] = reason
                        errors.append(reason)
                        after = _observe_clean(build_root, git_exe)
                        build_record["after"] = after
                        proof["builds"].append(build_record)
                        continue
                assert wheel is not None
                after = _observe_clean(build_root, git_exe)
                build_record["after"] = after
                if not after["ok"] or not after["clean"]:
                    reason = after.get("reason") or REASON_SOURCE_TREE_DIRTY
                    build_record["reason"] = reason
                    errors.append(reason)
                digest = sha256_file(wheel)
                build_record["wheel_filename"] = wheel.name
                build_record["wheel_sha256"] = digest
                # Keep a stable copy under artifacts/
                artifacts = attempt / "artifacts"
                artifacts.mkdir(exist_ok=True)
                preserved = artifacts / f"build{index}-{wheel.name}"
                shutil.copy2(wheel, preserved)
                build_record["preserved_relative"] = f"artifacts/{preserved.name}"
                digests.append(digest)
                names.append(wheel.name)
            except Exception as exc:  # noqa: BLE001 — record and continue second build
                reason = str(exc) or REASON_BUILD_FAILED
                log.error("build_exception index=%s reason=%s", index, reason)
                build_record["reason"] = reason
                errors.append(reason)
            finally:
                _remove_worktree(repo, build_root, git_exe)
            proof["builds"].append(build_record)

        if (
            len(digests) == 2
            and digests[0] == digests[1]
            and names[0] == names[1]
            and not errors
        ):
            proof["matching"] = True
            proof["wheel_filename"] = names[0]
            proof["wheel_sha256"] = digests[0]
        else:
            if len(digests) == 2 and digests[0] != digests[1]:
                errors.append(REASON_WHEEL_MISMATCH)
            proof["matching"] = False
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) or REASON_BUILD_FAILED
        log.error("build_aborted reason=%s", reason)
        errors.append(reason)
        proof["matching"] = False

    proof["errors"] = list(dict.fromkeys(errors))
    proof["duration_ms"] = int((time.monotonic() - started) * 1000)
    _atomic_write_json(attempt / "build-proof.json", proof)
    log.info(
        "build_done matching=%s errors=%s duration_ms=%s",
        proof["matching"],
        proof["errors"],
        proof["duration_ms"],
    )
    print(json.dumps({"attempt": attempt.name, "matching": proof["matching"]}, sort_keys=True))
    return 0 if proof["matching"] else 1


def _resolve_under(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("path_escape")
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("path_escape") from exc
    return target


def _binding_payload_without_id(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in manifest.items() if key != "artifact_id"}
    return payload


def build_artifact_binding(
    *,
    evidence_root: Path,
    proof: Mapping[str, Any],
    wheel_relative: str,
    lock_digest: str,
    pyproject_digest: str,
    tool: Mapping[str, Any],
) -> dict[str, Any]:
    wheel_path = _resolve_under(evidence_root, wheel_relative)
    if not wheel_path.is_file():
        raise ValueError(REASON_BINDING_INCOMPLETE)
    wheel_hash = sha256_file(wheel_path)
    if proof.get("wheel_sha256") not in {None, wheel_hash}:
        # When proof carries the matching digest it must equal the file.
        if proof.get("matching") and proof.get("wheel_sha256") != wheel_hash:
            raise ValueError(REASON_BINDING_MISMATCH)
    body = {
        "schema": SCHEMA_ARTIFACT_BINDING,
        "commit": proof.get("commit"),
        "tree": proof.get("tree"),
        "wheel_relative": wheel_relative,
        "wheel_sha256": wheel_hash,
        "lock_digest": lock_digest,
        "pyproject_digest": pyproject_digest,
        "tool": dict(tool),
        "build_proof": dict(proof),
        "build_proof_digest": canonical_digest(dict(proof)),
    }
    artifact_id = canonical_digest(body)
    return {**body, "artifact_id": artifact_id}


def _proof_clean_observations(proof: Mapping[str, Any]) -> bool:
    builds = proof.get("builds")
    if not isinstance(builds, list) or len(builds) != 2:
        return False
    for item in builds:
        if not isinstance(item, dict):
            return False
        for key in ("before", "after"):
            obs = item.get(key) or {}
            if not obs.get("ok") or not obs.get("clean"):
                return False
    return True


def _recheck_preserved_wheels(
    *,
    evidence_root: Path,
    proof: Mapping[str, Any],
    expected_sha256: str,
) -> list[str]:
    errors: list[str] = []
    builds = proof.get("builds")
    if not isinstance(builds, list) or len(builds) != 2:
        return [REASON_BINDING_INCOMPLETE]
    for item in builds:
        if not isinstance(item, dict):
            errors.append(REASON_BINDING_INCOMPLETE)
            continue
        digest = item.get("wheel_sha256")
        if digest != expected_sha256:
            errors.append(REASON_BINDING_MISMATCH)
        relative = item.get("preserved_relative")
        if isinstance(relative, str) and relative:
            try:
                path = _resolve_under(evidence_root, relative)
            except ValueError:
                errors.append(REASON_BINDING_INCOMPLETE)
                continue
            if not path.is_file() or sha256_file(path) != expected_sha256:
                errors.append(REASON_BINDING_MISMATCH)
    return list(dict.fromkeys(errors))


def verify_artifact_binding(
    *,
    manifest_path: Path,
    repo: Path,
    git_exe: str | None = None,
) -> dict[str, Any]:
    log.info("verify_binding_start manifest=%s", _redact_path(manifest_path))
    manifest = _load_json(manifest_path)
    errors: list[str] = []
    if manifest.get("schema") != SCHEMA_ARTIFACT_BINDING:
        errors.append(REASON_BINDING_INCOMPLETE)
    evidence_root = manifest_path.parent.resolve()
    try:
        wheel_rel = str(manifest["wheel_relative"])
        wheel_path = _resolve_under(evidence_root, wheel_rel)
    except (KeyError, ValueError):
        errors.append(REASON_BINDING_INCOMPLETE)
        wheel_path = None
    expected_id = manifest.get("artifact_id")
    recomputed = canonical_digest(_binding_payload_without_id(manifest))
    if expected_id != recomputed:
        errors.append(REASON_BINDING_MISMATCH)
    if wheel_path is not None and wheel_path.is_file():
        actual = sha256_file(wheel_path)
        if actual != manifest.get("wheel_sha256"):
            errors.append(REASON_BINDING_MISMATCH)
    else:
        errors.append(REASON_BINDING_INCOMPLETE)
    commit = manifest.get("commit")
    tree = manifest.get("tree")
    git = git_exe or shutil.which("git") or "git"
    if isinstance(commit, str) and FULL_OID_RE.fullmatch(commit):
        try:
            _require_full_commit(repo, commit, git)
            actual_tree = _commit_tree(repo, commit, git)
            if tree != actual_tree:
                errors.append(REASON_BINDING_MISMATCH)
            for name, key in (("uv.lock", "lock_digest"), ("pyproject.toml", "pyproject_digest")):
                blob = subprocess.run(
                    [git, "-C", str(repo), "show", f"{commit}:{name}"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout
                if sha256_bytes(blob) != manifest.get(key):
                    errors.append(REASON_BINDING_MISMATCH)
        except (subprocess.CalledProcessError, ValueError, RuntimeError):
            errors.append(REASON_BINDING_MISMATCH)
    else:
        errors.append(REASON_BINDING_INCOMPLETE)
    proof = manifest.get("build_proof")
    if not isinstance(proof, dict) or not proof.get("matching"):
        errors.append(REASON_BINDING_INCOMPLETE)
    else:
        if proof.get("wheel_sha256") != manifest.get("wheel_sha256"):
            errors.append(REASON_BINDING_MISMATCH)
        if not _proof_clean_observations(proof):
            errors.append(REASON_BINDING_INCOMPLETE)
        errors.extend(
            _recheck_preserved_wheels(
                evidence_root=evidence_root,
                proof=proof,
                expected_sha256=str(manifest.get("wheel_sha256") or ""),
            )
        )
    result = {
        "ok": not errors,
        "artifact_id": expected_id,
        "errors": list(dict.fromkeys(errors)),
        "schema": SCHEMA_ARTIFACT_BINDING,
    }
    log.info("verify_binding_done ok=%s errors=%s", result["ok"], result["errors"])
    return result


def _parse_wheel_record(wheel_path: Path) -> list[tuple[str, str | None, str | None]]:
    """Return RECORD rows as (path, hash_algo_digest, size)."""

    rows: list[tuple[str, str | None, str | None]] = []
    with zipfile.ZipFile(wheel_path) as archive:
        record_name = next(
            (
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/RECORD")
            ),
            None,
        )
        if record_name is None:
            raise ValueError("wheel_record_missing")
        text = archive.read(record_name).decode("utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        path = parts[0]
        digest = parts[1] if len(parts) > 1 and parts[1] else None
        size = parts[2] if len(parts) > 2 and parts[2] else None
        rows.append((path, digest, size))
    return rows


def _record_digest_matches(payload: bytes, recorded: str) -> bool:
    if not recorded.startswith("sha256="):
        return False
    encoded = recorded.split("=", 1)[1]
    padding = "=" * (-len(encoded) % 4)
    expected = base64.urlsafe_b64decode(encoded + padding)
    return hashlib.sha256(payload).digest() == expected


def verify_installed_wheel(
    *,
    wheel_path: Path,
    expected_sha256: str,
    site_packages: Path | None = None,
    editable_marker: Path | None = None,
) -> dict[str, Any]:
    """Revalidate a local wheel and optional installed layout (no network)."""

    log.debug("verify_installed_wheel path=%s", _redact_path(wheel_path))
    errors: list[str] = []
    if not wheel_path.is_file() or wheel_path.suffix != ".whl":
        errors.append(REASON_INSTALLED_UNVERIFIED)
    elif sha256_file(wheel_path) != expected_sha256:
        errors.append(REASON_BINDING_MISMATCH)
    if editable_marker is not None and editable_marker.exists():
        errors.append(REASON_INSTALLED_UNVERIFIED)
    if site_packages is not None:
        shadow = site_packages / "lean_weave.pth"
        if shadow.exists():
            errors.append(REASON_INSTALLED_UNVERIFIED)
        pkg = site_packages / "lean_weave"
        if pkg.exists() and pkg.is_symlink():
            errors.append(REASON_INSTALLED_UNVERIFIED)
        if wheel_path.is_file() and wheel_path.suffix == ".whl" and not errors:
            try:
                rows = _parse_wheel_record(wheel_path)
            except (ValueError, KeyError, zipfile.BadZipFile, OSError):
                errors.append(REASON_INSTALLED_UNVERIFIED)
                rows = []
            for relative, digest, _size in rows:
                is_pkg = relative.startswith("lean_weave/")
                is_meta = relative.startswith("lean_weave-") and ".dist-info/" in relative
                if not (is_pkg or is_meta):
                    continue
                target = site_packages / relative
                if not target.is_file() or target.is_symlink():
                    errors.append(REASON_INSTALLED_UNVERIFIED)
                    continue
                if digest and not _record_digest_matches(target.read_bytes(), digest):
                    errors.append(REASON_INSTALLED_UNVERIFIED)
    return {
        "ok": not errors,
        "errors": list(dict.fromkeys(errors)),
        "wheel_sha256": expected_sha256,
    }


def build_install_plan(
    *,
    python: str,
    uv: str,
    worktree: Path,
    wheelhouse: Path,
    export_path: Path,
    wheel_path: Path,
) -> dict[str, Any]:
    """Return offline uv export/sync/install argv without executing them."""

    cleared = [key for key in os.environ if key.startswith("UV_")]
    export_argv = [
        uv,
        "--no-config",
        "--offline",
        "export",
        "--project",
        str(worktree),
        "--locked",
        "--no-default-groups",
        "--no-emit-project",
        "--format",
        "requirements-txt",
    ]
    sync_argv = [
        uv,
        "--no-config",
        "--offline",
        "pip",
        "sync",
        "--python",
        python,
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--require-hashes",
        str(export_path),
    ]
    install_argv = [
        uv,
        "--no-config",
        "--offline",
        "pip",
        "install",
        "--python",
        python,
        "--no-deps",
        str(wheel_path),
    ]
    return {
        "clear_uv_env": cleared,
        "export_argv": export_argv,
        "sync_argv": sync_argv,
        "install_argv": install_argv,
        "wheel_sha256": sha256_file(wheel_path) if wheel_path.is_file() else None,
    }


def verify_network_namespace(launcher: Sequence[str] | None = None) -> dict[str, Any]:
    """Probe an unshare-based network namespace; never invent success."""

    argv = list(launcher) if launcher is not None else [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--",
        "true",
    ]
    if not argv:
        return {"ok": False, "reason": "namespace_unavailable"}
    exe = shutil.which(str(argv[0]))
    if exe is None:
        return {"ok": False, "reason": "namespace_unavailable"}
    try:
        completed = subprocess.run(
            [exe, *argv[1:]],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return {"ok": False, "reason": "namespace_unavailable"}
    if completed.returncode != 0:
        return {"ok": False, "reason": "namespace_unavailable"}
    return {"ok": True, "reason": None, "launcher_digest": canonical_digest(list(argv))}


def verify_ed25519_detached(
    *,
    openssl: str,
    public_key: Path,
    message: Path,
    signature: Path,
) -> bool:
    """Verify a detached Ed25519 signature with a pinned openssl binary."""

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


def aggregate_measured_usage(dispatches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate provider-reported usage; never substitute estimates."""

    total_input = 0
    total_output = 0
    total_tokens = 0
    dispatched = 0
    successes = 0
    incomplete = False
    for row in dispatches:
        if row.get("no_send") or row.get("estimate_only"):
            continue
        if not row.get("dispatched"):
            continue
        dispatched += 1
        try:
            inp = int(row["input_tokens"])
            out = int(row["output_tokens"])
            tot = int(row.get("total_tokens", inp + out))
        except (KeyError, TypeError, ValueError):
            incomplete = True
            continue
        if tot != inp + out and "total_tokens" in row:
            # Prefer explicit total when provided; still book components.
            pass
        total_input += inp
        total_output += out
        total_tokens += tot
        if row.get("success"):
            successes += 1
    coverage_complete = dispatched > 0 and not incomplete
    per_success = None
    if coverage_complete and successes > 0:
        per_success = total_tokens / float(successes)
    return {
        "dispatched": dispatched,
        "successes": successes,
        "input_tokens": total_input if coverage_complete else None,
        "output_tokens": total_output if coverage_complete else None,
        "total_tokens": total_tokens if coverage_complete else None,
        "tokens_per_success": per_success,
        "coverage_complete": coverage_complete,
    }


def cmd_verify_binding(args: argparse.Namespace) -> int:
    result = verify_artifact_binding(
        manifest_path=Path(args.manifest).resolve(),
        repo=Path(args.repo).resolve(),
        git_exe=args.git,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def freeze_run(
    *,
    manifest_path: Path,
    preregistration_path: Path,
    output: Path,
    run_nonce: str | None = None,
) -> dict[str, Any]:
    log.info("freeze_run_start output=%s", _redact_path(output))
    manifest = _load_json(manifest_path)
    prereg = _load_json(preregistration_path)
    if manifest.get("schema") != SCHEMA_ARTIFACT_BINDING:
        raise ValueError(REASON_BINDING_INCOMPLETE)
    artifact_id = manifest.get("artifact_id")
    if not artifact_id:
        raise ValueError(REASON_BINDING_INCOMPLETE)
    nonce = run_nonce or uuid.uuid4().hex
    stages = prereg.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("preregistration_stages_missing")
    policy_digest = prereg.get("policy_digest")
    custodian = prereg.get("custodian") or {}
    payload = {
        "schema_version": SCHEMA_RELEASE_RUN,
        "run_nonce": nonce,
        "artifact_id": artifact_id,
        "policy_digest": policy_digest,
        "custodian_key_fingerprint": custodian.get("key_fingerprint"),
        "custodian_approval_id": custodian.get("approval_id"),
        "stages": stages,
    }
    run_id = canonical_digest(payload)
    run_dir = output / f"run-{run_id[-12:]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    record = {
        "schema": SCHEMA_RELEASE_RUN,
        "run_id": run_id,
        "artifact_id": artifact_id,
        "commit": manifest.get("commit"),
        "wheel_sha256": manifest.get("wheel_sha256"),
        "policy_digest": policy_digest,
        "custodian": {
            "key_fingerprint": custodian.get("key_fingerprint"),
            "approval_id": custodian.get("approval_id"),
        },
        "stages": stages,
        "run_nonce": nonce,
        "manifest_digest": sha256_file(manifest_path),
        "preregistration_digest": sha256_file(preregistration_path),
    }
    _atomic_write_json(run_dir / "release-run.json", record)
    pin = {
        "run_id": run_id,
        "artifact_id": artifact_id,
        "created_from": {
            "manifest_digest": record["manifest_digest"],
            "preregistration_digest": record["preregistration_digest"],
        },
    }
    # Operator-controlled pin sibling; decide requires this independent file.
    pin_path = output / "run-id-pin.json"
    if pin_path.exists():
        raise FileExistsError("duplicate_run_id_pin")
    _atomic_write_json(pin_path, pin)
    log.info("freeze_run_done run_id=%s", run_id)
    return {"run_id": run_id, "run_dir": str(run_dir.name), "pin": pin}


def cmd_freeze_run(args: argparse.Namespace) -> int:
    try:
        result = freeze_run(
            manifest_path=Path(args.manifest).resolve(),
            preregistration_path=Path(args.preregistration).resolve(),
            output=Path(args.output).resolve(),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("freeze_run_failed reason=%s", exc)
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


def make_envelope(
    *,
    run_id: str,
    stage_id: str,
    artifact_id: str,
    commit: str | None,
    wheel_sha256: str | None,
    config_digest: str | None,
    dataset_digest: str | None,
    runner_mode: str,
    output_hashes: Mapping[str, str],
    status: str,
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_EVIDENCE_ENVELOPE,
        "run_id": run_id,
        "stage_id": stage_id,
        "artifact_id": artifact_id,
        "commit": commit,
        "wheel_sha256": wheel_sha256,
        "config_digest": config_digest,
        "dataset_digest": dataset_digest,
        "runner_mode": runner_mode,
        "output_hashes": dict(output_hashes),
        "status": status,
        "reason_codes": list(reason_codes),
    }


def publish_envelope(root: Path, relative: str, envelope: Mapping[str, Any]) -> Path:
    target = _resolve_under(root, relative)
    if target.exists():
        raise FileExistsError("duplicate_publication")
    _atomic_write_json(target, envelope)
    return target


def _ledger_covers_required(
    *,
    ledger_path: Path,
    required_nodes: Sequence[str],
) -> list[str]:
    """Return reason codes when required pytest nodes are missing or non-passing."""

    ledger = _load_json(ledger_path)
    outcomes = ledger.get("outcomes") or ledger.get("nodes") or {}
    if isinstance(outcomes, list):
        mapped = {
            str(item.get("nodeid")): item
            for item in outcomes
            if isinstance(item, dict) and item.get("nodeid")
        }
    elif isinstance(outcomes, dict):
        mapped = outcomes
    else:
        return [REASON_SCHEDULE_INCOMPLETE]
    reasons: list[str] = []
    for nodeid in required_nodes:
        item = mapped.get(nodeid)
        if item is None:
            reasons.append(REASON_REQUIRED_NODE_MISSING)
            continue
        status = str(item.get("outcome") or item.get("status") or "").lower()
        if status in {"skipped", "xfail", "xpassed"}:
            reasons.append(REASON_REQUIRED_NODE_MISSING)
        elif status not in {"passed", "pass", "ok"}:
            reasons.append(REASON_OFFLINE_FAILED)
    return list(dict.fromkeys(reasons))


def cmd_offline(args: argparse.Namespace) -> int:
    """Record an offline evidence envelope from operator-supplied receipts.

    Real pytest / installed-matrix execution is operator-driven; this command
    validates binding and publishes a create-once OfflineReceiptV1.
    """

    evidence_root = Path(args.evidence_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    pin_path = Path(args.expected_run_id_file).resolve()
    binding = verify_artifact_binding(
        manifest_path=manifest_path,
        repo=Path(args.repo).resolve(),
        git_exe=args.git,
    )
    pin = _load_json(pin_path)
    run_id = pin.get("run_id")
    source_report = Path(args.source_report).resolve() if args.source_report else None
    installed_report = (
        Path(args.installed_report).resolve() if args.installed_report else None
    )
    reason_codes: list[str] = []
    status = "pass"
    if not binding["ok"]:
        status = "fail"
        reason_codes.extend(binding["errors"] or [REASON_BINDING_MISMATCH])
    if source_report is None or not source_report.is_file():
        status = "fail"
        reason_codes.append(REASON_OFFLINE_FAILED)
        source_hash = None
    else:
        source_hash = sha256_file(source_report)
    if installed_report is None or not installed_report.is_file():
        status = "fail"
        reason_codes.append(REASON_SCHEDULE_INCOMPLETE)
        installed_hash = None
    else:
        installed_hash = sha256_file(installed_report)
    if args.required_nodes_missing:
        status = "fail"
        reason_codes.append(REASON_REQUIRED_NODE_MISSING)
    ledger_hash = None
    required_digest = args.required_nodes_digest
    if args.required_nodes_file:
        required_path = Path(args.required_nodes_file).resolve()
        required_nodes = json.loads(required_path.read_text(encoding="utf-8"))
        if not isinstance(required_nodes, list):
            status = "fail"
            reason_codes.append(REASON_SCHEDULE_INCOMPLETE)
            required_nodes = []
        required_digest = required_digest or sha256_file(required_path)
        if args.ledger_path:
            ledger_path = Path(args.ledger_path).resolve()
            if not ledger_path.is_file():
                status = "fail"
                reason_codes.append(REASON_SCHEDULE_INCOMPLETE)
            else:
                ledger_hash = sha256_file(ledger_path)
                reason_codes.extend(
                    _ledger_covers_required(
                        ledger_path=ledger_path,
                        required_nodes=[str(item) for item in required_nodes],
                    )
                )
                if reason_codes:
                    status = "fail"
        else:
            status = "fail"
            reason_codes.append(REASON_SCHEDULE_INCOMPLETE)
    if args.require_network_namespace:
        namespace = verify_network_namespace(
            args.namespace_launcher.split() if args.namespace_launcher else None
        )
        if not namespace.get("ok"):
            status = "fail"
            reason_codes.append("namespace_unavailable")
    before = args.artifact_sha_before
    after = args.artifact_sha_after
    if before is not None and after is not None and before != after:
        status = "fail"
        reason_codes.append(REASON_ARTIFACT_CHANGED)
    receipt = {
        "schema": SCHEMA_OFFLINE_RECEIPT,
        "envelope": make_envelope(
            run_id=str(run_id),
            stage_id="offline",
            artifact_id=str(binding.get("artifact_id") or pin.get("artifact_id")),
            commit=_load_json(manifest_path).get("commit"),
            wheel_sha256=_load_json(manifest_path).get("wheel_sha256"),
            config_digest=args.config_digest,
            dataset_digest=args.dataset_digest,
            runner_mode="installed-wheel" if installed_hash else "source-regression",
            output_hashes={
                key: value
                for key, value in {
                    "source_report": source_hash,
                    "installed_report": installed_hash,
                    "ledger": ledger_hash,
                }.items()
                if value
            },
            status=status,
            reason_codes=list(dict.fromkeys(reason_codes)),
        ),
        "source_runtime": "source-regression",
        "installed_runtime": "installed-wheel",
        "required_nodes_digest": required_digest,
    }
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        publish_envelope(out, "offline-receipt.json", receipt)
    except FileExistsError:
        log.error("offline_duplicate")
        return 1
    print(json.dumps({"ok": status == "pass", "status": status}, sort_keys=True))
    return 0 if status == "pass" else 1


def cmd_live(args: argparse.Namespace) -> int:
    """Record live disposition without inventing usage."""

    policy = _load_json(Path(args.policy).resolve())
    live_mode = policy.get("live")
    pin = _load_json(Path(args.expected_run_id_file).resolve())
    authorized = bool(args.authorized)
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    reason_codes: list[str] = []
    state = "not_evaluated"
    measured = {
        "dispatched": 0,
        "successes": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "tokens_per_success": None,
        "coverage_complete": False,
    }
    if args.usage_json:
        usage_path = Path(args.usage_json).resolve()
        payload = _load_json(usage_path)
        rows = payload.get("dispatches") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            state = "invalid_or_incomplete"
            reason_codes.append("live_usage_incomplete")
            rows = []
        measured = aggregate_measured_usage(rows)
    if live_mode not in {"optional", "required"}:
        state = "invalid_or_incomplete"
        reason_codes.append("live_policy_invalid")
    elif not authorized or not has_key:
        state = "not_evaluated"
        reason_codes.append("live_not_authorized" if not authorized else "live_key_missing")
    elif args.runner_qualified is False:
        state = "not_evaluated"
        reason_codes.append("live_runner_not_qualified")
    elif args.dispatch_occurred and (
        args.usage_complete is False or not measured.get("coverage_complete")
    ):
        state = "invalid_or_incomplete"
        reason_codes.append("live_usage_incomplete")
    elif args.blocking_regression:
        state = "evaluated_fail"
        reason_codes.append("live_blocking_regression")
    elif args.dispatch_occurred and args.usage_complete and measured.get("coverage_complete"):
        state = "evaluated_pass"
    else:
        state = "not_evaluated"
        reason_codes.append("live_not_dispatched")

    if live_mode == "required" and state != "evaluated_pass":
        # Required live cannot pass without a complete measured run.
        if state == "not_evaluated":
            reason_codes.append("live_required_unevaluated")

    receipt = {
        "schema": SCHEMA_LIVE_RECEIPT,
        "envelope": make_envelope(
            run_id=str(pin.get("run_id")),
            stage_id="live",
            artifact_id=str(pin.get("artifact_id")),
            commit=None,
            wheel_sha256=None,
            config_digest=policy.get("digest"),
            dataset_digest=None,
            runner_mode="live",
            output_hashes={},
            status=state,
            reason_codes=list(dict.fromkeys(reason_codes)),
        ),
        "policy_live": live_mode,
        "state": state,
        "measured_usage_available": bool(
            measured.get("coverage_complete") and state == "evaluated_pass"
        ),
        "dispatch_occurred": bool(args.dispatch_occurred),
        "measured_usage": measured,
    }
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        publish_envelope(out, "live-receipt.json", receipt)
    except FileExistsError:
        return 1
    print(json.dumps({"ok": True, "state": state, "Live": _live_label(state, live_mode)}, sort_keys=True))
    return 0


def _live_label(state: str, live_mode: str | None) -> str:
    if state == "not_evaluated":
        return "Not evaluated"
    if state == "invalid_or_incomplete":
        return "Inconclusive"
    if state == "evaluated_fail":
        return "Failed"
    if state == "evaluated_pass":
        return "Pass"
    return "Not evaluated"


def _gate(
    *,
    status: str,
    detail: str | None = None,
) -> dict[str, Any]:
    return {"status": status, "detail": detail}


def decide_release(
    *,
    repo: Path,
    commit: str,
    evidence_root: Path,
    policy_path: Path,
    output: Path,
    manifest_path: Path | None,
    expected_run_id: str | None,
    git_exe: str | None = None,
) -> tuple[dict[str, Any], int]:
    log.info("decide_start commit=%s", commit[:12] if commit else "?")
    policy = _load_json(policy_path) if policy_path.is_file() else {}
    live_policy = policy.get("live")
    gates: dict[str, dict[str, Any]] = {
        "build": _gate(status="missing"),
        "binding": _gate(status="missing"),
        "offline": _gate(status="missing"),
        "blind": _gate(status="missing"),
        "live": _gate(status="missing"),
    }
    issues: list[str] = []
    verified_commit = None
    wheel_hash = None
    artifact_id = None
    run_id = expected_run_id

    if manifest_path is None or not manifest_path.is_file():
        gates["binding"] = _gate(status="missing", detail="manifest_absent")
        issues.append("manifest_missing")
    else:
        binding = verify_artifact_binding(
            manifest_path=manifest_path,
            repo=repo,
            git_exe=git_exe,
        )
        if binding["ok"]:
            gates["binding"] = _gate(status="pass")
            manifest = _load_json(manifest_path)
            verified_commit = manifest.get("commit")
            wheel_hash = manifest.get("wheel_sha256")
            artifact_id = manifest.get("artifact_id")
            proof = manifest.get("build_proof") or {}
            if proof.get("matching"):
                gates["build"] = _gate(status="pass")
            else:
                gates["build"] = _gate(status="fail", detail="build_not_matching")
                issues.append("build_not_matching")
        else:
            gates["binding"] = _gate(status="fail", detail=",".join(binding["errors"]))
            gates["build"] = _gate(status="invalid", detail="binding_failed")
            issues.extend(binding["errors"])

    if verified_commit and commit and verified_commit != commit:
        gates["binding"] = _gate(status="fail", detail="commit_mismatch")
        issues.append("commit_mismatch")

    offline_path = evidence_root / "offline-receipt.json"
    if offline_path.is_file():
        offline = _load_json(offline_path)
        env = offline.get("envelope") or {}
        if run_id and env.get("run_id") != run_id:
            gates["offline"] = _gate(status="invalid", detail="run_id_mismatch")
            issues.append("offline_run_mismatch")
        elif run_id and not env.get("run_id"):
            gates["offline"] = _gate(status="invalid", detail="run_id_missing")
            issues.append("offline_run_mismatch")
        elif env.get("status") == "pass":
            gates["offline"] = _gate(status="pass")
        else:
            gates["offline"] = _gate(status="fail", detail=",".join(env.get("reason_codes") or []))
            issues.append("offline_failed")
    else:
        gates["offline"] = _gate(status="missing")
        issues.append("offline_missing")

    blind_path = evidence_root / "blind-receipt.json"
    require_sig = bool(policy.get("require_blind_signature"))
    if blind_path.is_file():
        blind = _load_json(blind_path)
        env = blind.get("envelope") or {}
        if run_id and env.get("run_id") != run_id:
            gates["blind"] = _gate(status="invalid", detail="run_id_mismatch")
            issues.append("blind_run_mismatch")
        elif run_id and not env.get("run_id"):
            gates["blind"] = _gate(status="invalid", detail="run_id_missing")
            issues.append("blind_run_mismatch")
        else:
            sig_ok = True
            if require_sig:
                pubkey = policy.get("custodian_pubkey")
                signature = policy.get("blind_signature_path")
                openssl = policy.get("openssl") or shutil.which("openssl") or "openssl"
                if not pubkey or not signature:
                    sig_ok = False
                    issues.append(REASON_HOLDOUT_INVALID)
                else:
                    sig_ok = verify_ed25519_detached(
                        openssl=str(openssl),
                        public_key=Path(str(pubkey)),
                        message=blind_path,
                        signature=Path(str(signature)),
                    )
                    if not sig_ok:
                        issues.append(REASON_HOLDOUT_INVALID)
            if not sig_ok:
                gates["blind"] = _gate(status="invalid", detail=REASON_HOLDOUT_INVALID)
            elif env.get("status") == "pass":
                gates["blind"] = _gate(status="pass")
            else:
                gates["blind"] = _gate(
                    status="fail",
                    detail=",".join(env.get("reason_codes") or []),
                )
                issues.append("blind_failed")
    else:
        gates["blind"] = _gate(status="missing", detail=REASON_HOLDOUT_MISSING)
        issues.append(REASON_HOLDOUT_MISSING)

    live_path = evidence_root / "live-receipt.json"
    if live_path.is_file():
        live = _load_json(live_path)
        state = live.get("state") or (live.get("envelope") or {}).get("status")
        if live_policy == "optional" and state == "not_evaluated":
            gates["live"] = _gate(status="not_required", detail="Live: Not evaluated")
        elif live_policy == "optional" and state == "invalid_or_incomplete":
            gates["live"] = _gate(status="not_required", detail="Live: Inconclusive")
            issues.append("live_inconclusive_no_savings_claim")
        elif live_policy == "required" and state != "evaluated_pass":
            gates["live"] = _gate(status="fail", detail=str(state))
            issues.append("live_required_unsatisfied")
        elif state == "evaluated_fail":
            gates["live"] = _gate(status="fail", detail="blocking_regression")
            issues.append("live_blocking_regression")
        elif state == "evaluated_pass":
            gates["live"] = _gate(status="pass")
        else:
            gates["live"] = _gate(status="invalid", detail=str(state))
            issues.append("live_invalid")
    else:
        if live_policy == "optional":
            gates["live"] = _gate(status="not_required", detail="Live: Not evaluated")
        elif live_policy == "required":
            gates["live"] = _gate(status="missing")
            issues.append("live_required_missing")
        else:
            gates["live"] = _gate(status="invalid", detail="policy_missing")
            issues.append("live_policy_missing")

    required_ok = all(
        gates[name]["status"] in {"pass", "not_required"}
        for name in ("build", "binding", "offline", "blind", "live")
    )
    verdict = "Ready (for opt-in)" if required_ok else "Not ready"
    decision = {
        "schema": SCHEMA_RELEASE_DECISION,
        "requested_commit": commit or None,
        "verified_commit": verified_commit,
        "wheel_sha256": wheel_hash,
        "artifact_id": artifact_id,
        "run_id": run_id,
        "gates": gates,
        "live_label": (
            (gates["live"].get("detail") if gates["live"]["status"] == "not_required" else gates["live"]["status"])
        ),
        "unresolved_issues": issues,
        "verdict": verdict,
        "measured_token_savings_claimed": False,
    }
    attempt = output / f"decision-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(parents=True, exist_ok=False)
    _atomic_write_json(attempt / "release-decision.json", decision)
    md = _decision_markdown(decision)
    (attempt / "release-decision.md").write_text(md, encoding="utf-8")
    log.info("decide_done verdict=%s", verdict)
    return decision, (0 if verdict.startswith("Ready") else 1)


def _decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "# V2 Release Decision",
        "",
        f"Verdict: **{decision['verdict']}**",
        "",
        f"- Requested commit: `{decision.get('requested_commit')}`",
        f"- Verified commit: `{decision.get('verified_commit')}`",
        f"- Wheel: `{decision.get('wheel_sha256')}`",
        f"- Artifact ID: `{decision.get('artifact_id')}`",
        f"- Run ID: `{decision.get('run_id')}`",
        "",
        "## Gates",
        "",
    ]
    for name, gate in (decision.get("gates") or {}).items():
        lines.append(f"- {name}: `{gate.get('status')}` ({gate.get('detail')})")
    lines.extend(
        [
            "",
            "## Unresolved issues",
            "",
        ]
    )
    issues = decision.get("unresolved_issues") or []
    if not issues:
        lines.append("- none")
    else:
        for item in issues:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            f"Measured token savings claimed: `{decision.get('measured_token_savings_claimed')}`",
            "",
        ]
    )
    return "\n".join(lines)


def cmd_decide(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest).resolve() if args.manifest else None
    expected = args.expected_run_id
    if args.expected_run_id_file:
        pin = Path(args.expected_run_id_file)
        if pin.is_file():
            expected = _load_json(pin.resolve()).get("run_id")
        elif expected is None:
            expected = None
    try:
        decision, code = decide_release(
            repo=Path(args.repo).resolve(),
            commit=args.commit,
            evidence_root=Path(args.evidence_root).resolve(),
            policy_path=Path(args.policy).resolve(),
            output=Path(args.output).resolve(),
            manifest_path=manifest,
            expected_run_id=expected,
            git_exe=args.git,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("decide_malformed reason=%s", exc)
        return 2
    print(json.dumps({"verdict": decision["verdict"], "gates": decision["gates"]}, sort_keys=True))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_evidence")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Double-build from a clean full commit OID")
    build.add_argument("--repo", required=True)
    build.add_argument("--commit", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--build-inputs", required=True)
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser("verify-binding", help="Revalidate artifact-binding.json")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--repo", required=True)
    verify.add_argument("--git", default=None)
    verify.set_defaults(func=cmd_verify_binding)

    freeze = sub.add_parser("freeze-run", help="Create ReleaseRunV1 before evidence")
    freeze.add_argument("--manifest", required=True)
    freeze.add_argument("--preregistration", required=True)
    freeze.add_argument("--output", required=True)
    freeze.set_defaults(func=cmd_freeze_run)

    offline = sub.add_parser("offline", help="Publish OfflineReceiptV1")
    offline.add_argument("--repo", required=True)
    offline.add_argument("--manifest", required=True)
    offline.add_argument("--evidence-root", required=True)
    offline.add_argument("--expected-run-id-file", required=True)
    offline.add_argument("--output", required=True)
    offline.add_argument("--source-report", default=None)
    offline.add_argument("--installed-report", default=None)
    offline.add_argument("--config-digest", default=None)
    offline.add_argument("--dataset-digest", default=None)
    offline.add_argument("--required-nodes-digest", default=None)
    offline.add_argument("--required-nodes-missing", action="store_true")
    offline.add_argument("--required-nodes-file", default=None)
    offline.add_argument("--ledger-path", default=None)
    offline.add_argument("--require-network-namespace", action="store_true")
    offline.add_argument("--namespace-launcher", default=None)
    offline.add_argument("--artifact-sha-before", default=None)
    offline.add_argument("--artifact-sha-after", default=None)
    offline.add_argument("--git", default=None)
    offline.set_defaults(func=cmd_offline)

    live = sub.add_parser("live", help="Publish LiveReceiptV1 disposition")
    live.add_argument("--policy", required=True)
    live.add_argument("--expected-run-id-file", required=True)
    live.add_argument("--output", required=True)
    live.add_argument("--authorized", action="store_true")
    live.add_argument("--runner-qualified", action=argparse.BooleanOptionalAction, default=False)
    live.add_argument("--dispatch-occurred", action="store_true")
    live.add_argument("--usage-complete", action="store_true")
    live.add_argument("--blocking-regression", action="store_true")
    live.add_argument("--usage-json", default=None)
    live.set_defaults(func=cmd_live)

    decide = sub.add_parser("decide", help="Fail-closed release eligibility verdict")
    decide.add_argument("--repo", required=True)
    decide.add_argument("--commit", required=True)
    decide.add_argument("--evidence-root", required=True)
    decide.add_argument("--policy", required=True)
    decide.add_argument("--output", required=True)
    decide.add_argument("--manifest", default=None)
    decide.add_argument("--expected-run-id", default=None)
    decide.add_argument("--expected-run-id-file", default=None)
    decide.add_argument("--git", default=None)
    decide.set_defaults(func=cmd_decide)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    log.debug("command=%s", args.command)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
