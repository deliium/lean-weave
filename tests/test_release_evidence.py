"""Synthetic tests for the stdlib release-evidence driver."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from benchmarks import release_evidence as re


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "release@example.test")
    _git(repo, "config", "user.name", "Release Evidence")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "lean-weave"
            version = "0.1.0"
            requires-python = ">=3.12"
            [build-system]
            requires = ["uv_build>=0.13.6,<0.14.0"]
            build-backend = "uv_build"
            """
        ),
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("# lock\n", encoding="utf-8")
    driver_src = Path(__file__).resolve().parents[1] / "benchmarks" / "release_evidence.py"
    dest = repo / "benchmarks"
    dest.mkdir()
    shutil.copy2(driver_src, dest / "release_evidence.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")
    return repo


def _full_oid(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _build_inputs(path: Path, *, payload_hex: str, filename: str = "lean_weave-0.1.0-py3-none-any.whl") -> Path:
    data = {
        "mode": "synthetic",
        "python": shutil.which("python3") or "python3",
        "git": shutil.which("git") or "git",
        "uv": shutil.which("uv") or "uv",
        "wheel_filename": filename,
        "synthetic_wheel_bytes": payload_hex,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_canonical_digest_matches_conditions():
    from lean_weave.benchmark.conditions import canonical_digest as product

    vectors = [
        "plain",
        {"b": 2, "a": 1},
        [{"z": 1}, {"a": [3, 2]}],
    ]
    for item in vectors:
        assert re.canonical_digest(item) == product(item)


def test_build_ignores_dirty_authoring_tree(tmp_path):
    repo = _init_repo(tmp_path)
    # Dirty authoring tree must not affect detached builds.
    (repo / "README.md").write_text("# dirty authoring\n", encoding="utf-8")
    commit = _full_oid(repo)
    inputs = _build_inputs(tmp_path / "inputs.json", payload_hex=b"wheel-a".hex())
    out = tmp_path / "evidence"
    out.mkdir()
    code = re.main(
        [
            "build",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--output",
            str(out),
            "--build-inputs",
            str(inputs),
        ]
    )
    assert code == 0
    proofs = list(out.glob("build-*/build-proof.json"))
    assert len(proofs) == 1
    proof = json.loads(proofs[0].read_text(encoding="utf-8"))
    assert proof["matching"] is True
    assert proof["wheel_sha256"] == re.sha256_bytes(b"wheel-a")
    assert all(item["before"]["clean"] and item["after"]["clean"] for item in proof["builds"])


def test_build_rejects_short_sha_and_ref(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    inputs = _build_inputs(tmp_path / "inputs.json", payload_hex=b"x".hex())
    out = tmp_path / "evidence"
    out.mkdir()
    short = commit[:12]
    code = re.main(
        [
            "build",
            "--repo",
            str(repo),
            "--commit",
            short,
            "--output",
            str(out),
            "--build-inputs",
            str(inputs),
        ]
    )
    assert code == 1
    proof = json.loads(next(out.glob("build-*/build-proof.json")).read_text(encoding="utf-8"))
    assert re.REASON_COMMIT_INVALID in proof["errors"]


def test_build_records_wheel_mismatch(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    inputs = _build_inputs(tmp_path / "inputs.json", payload_hex=b"same".hex())
    out = tmp_path / "evidence"
    out.mkdir()

    original = re._write_synthetic_wheel
    calls = {"n": 0}

    def flaky(directory, filename, payload):
        calls["n"] += 1
        if calls["n"] == 2:
            payload = b"other"
        return original(directory, filename, payload)

    monkeypatch.setattr(re, "_write_synthetic_wheel", flaky)
    code = re.main(
        [
            "build",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--output",
            str(out),
            "--build-inputs",
            str(inputs),
        ]
    )
    assert code == 1
    proof = json.loads(next(out.glob("build-*/build-proof.json")).read_text(encoding="utf-8"))
    assert re.REASON_WHEEL_MISMATCH in proof["errors"]
    assert proof["matching"] is False


def _matching_proof(commit: str, tree: str, wheel: Path, evidence: Path) -> dict:
    digest = re.sha256_file(wheel)
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    preserved = []
    for index in (1, 2):
        copy = artifacts / f"build{index}-{wheel.name}"
        copy.write_bytes(wheel.read_bytes())
        preserved.append(f"artifacts/{copy.name}")
    return {
        "schema": re.SCHEMA_BUILD_PROOF,
        "commit": commit,
        "tree": tree,
        "matching": True,
        "wheel_filename": wheel.name,
        "wheel_sha256": digest,
        "builds": [
            {
                "index": 1,
                "before": {"ok": True, "clean": True},
                "after": {"ok": True, "clean": True},
                "wheel_sha256": digest,
                "preserved_relative": preserved[0],
            },
            {
                "index": 2,
                "before": {"ok": True, "clean": True},
                "after": {"ok": True, "clean": True},
                "wheel_sha256": digest,
                "preserved_relative": preserved[1],
            },
        ],
        "errors": [],
    }


def test_binding_and_freeze_roundtrip(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "lean_weave-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"bound-wheel")
    lock_digest = re.sha256_bytes((repo / "uv.lock").read_bytes())
    py_digest = re.sha256_bytes((repo / "pyproject.toml").read_bytes())
    proof = _matching_proof(commit, tree, wheel, evidence)
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=proof,
        wheel_relative="artifacts/lean_weave-0.1.0-py3-none-any.whl",
        lock_digest=lock_digest,
        pyproject_digest=py_digest,
        tool={"mode": "synthetic"},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    result = re.verify_artifact_binding(manifest_path=manifest, repo=repo)
    assert result["ok"] is True

    # Mutated wheel fails.
    wheel.write_bytes(b"mutated")
    bad = re.verify_artifact_binding(manifest_path=manifest, repo=repo)
    assert bad["ok"] is False
    assert re.REASON_BINDING_MISMATCH in bad["errors"]
    wheel.write_bytes(b"bound-wheel")

    prereg = evidence / "prereg.json"
    prereg.write_text(
        json.dumps(
            {
                "policy_digest": "sha256:" + "a" * 64,
                "custodian": {
                    "key_fingerprint": "sha256:" + "b" * 64,
                    "approval_id": "approval-1",
                },
                "stages": [
                    {"id": "offline", "tool": "pytest"},
                    {"id": "blind", "tool": "blind-capture"},
                    {"id": "live", "tool": "live"},
                ],
            }
        ),
        encoding="utf-8",
    )
    freeze_out = evidence / "runs"
    freeze_out.mkdir()
    frozen = re.freeze_run(
        manifest_path=manifest,
        preregistration_path=prereg,
        output=freeze_out,
        run_nonce="fixed-nonce",
    )
    assert frozen["run_id"].startswith("sha256:")
    pin = json.loads((freeze_out / "run-id-pin.json").read_text(encoding="utf-8"))
    assert pin["run_id"] == frozen["run_id"]

    # Path escape rejected.
    with pytest.raises(ValueError, match="path_escape"):
        re._resolve_under(evidence, "../escape.whl")


def test_duplicate_publication_rejected(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    envelope = re.make_envelope(
        run_id="sha256:" + "c" * 64,
        stage_id="offline",
        artifact_id="sha256:" + "d" * 64,
        commit=None,
        wheel_sha256=None,
        config_digest=None,
        dataset_digest=None,
        runner_mode="source-regression",
        output_hashes={},
        status="pass",
        reason_codes=[],
    )
    re.publish_envelope(root, "offline-receipt.json", envelope)
    with pytest.raises(FileExistsError):
        re.publish_envelope(root, "offline-receipt.json", envelope)


def test_decide_missing_evidence_is_not_ready(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "optional"}), encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    out = tmp_path / "decisions"
    out.mkdir()
    decision, code = re.decide_release(
        repo=repo,
        commit=commit,
        evidence_root=evidence,
        policy_path=policy,
        output=out,
        manifest_path=None,
        expected_run_id=None,
    )
    assert code == 1
    assert decision["verdict"] == "Not ready"
    assert decision["gates"]["binding"]["status"] == "missing"
    assert decision["gates"]["blind"]["status"] == "missing"
    assert decision["gates"]["live"]["status"] == "not_required"
    assert decision["measured_token_savings_claimed"] is False


def test_live_optional_without_auth_is_not_evaluated(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    pin = tmp_path / "pin.json"
    pin.write_text(
        json.dumps({"run_id": "sha256:" + "e" * 64, "artifact_id": "sha256:" + "f" * 64}),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "optional", "digest": "sha256:" + "1" * 64}), encoding="utf-8")
    out = tmp_path / "live"
    code = re.main(
        [
            "live",
            "--policy",
            str(policy),
            "--expected-run-id-file",
            str(pin),
            "--output",
            str(out),
        ]
    )
    assert code == 0
    receipt = json.loads((out / "live-receipt.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "not_evaluated"


def _write_minimal_wheel(path: Path, *, package_bytes: bytes = b"print(1)\n") -> Path:
    import base64
    import hashlib
    import zipfile

    record_hash = "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(package_bytes).digest()
    ).decode("ascii").rstrip("=")
    record = (
        f"lean_weave/__init__.py,{record_hash},{len(package_bytes)}\n"
        "lean_weave-0.1.0.dist-info/RECORD,,\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("lean_weave/__init__.py", package_bytes)
        archive.writestr("lean_weave-0.1.0.dist-info/RECORD", record)
    return path


def test_installed_wheel_rejects_shadow_and_editable(tmp_path):
    wheel = _write_minimal_wheel(tmp_path / "pkg.whl")
    digest = re.sha256_file(wheel)
    site = tmp_path / "site"
    site.mkdir()
    (site / "lean_weave.pth").write_text("shadow\n", encoding="utf-8")
    bad = re.verify_installed_wheel(
        wheel_path=wheel,
        expected_sha256=digest,
        site_packages=site,
    )
    assert bad["ok"] is False
    assert re.REASON_INSTALLED_UNVERIFIED in bad["errors"]

    ok = re.verify_installed_wheel(
        wheel_path=wheel,
        expected_sha256=digest,
        site_packages=None,
    )
    assert ok["ok"] is True
    editable = tmp_path / "lean_weave.egg-link"
    editable.write_text("x", encoding="utf-8")
    bad2 = re.verify_installed_wheel(
        wheel_path=wheel,
        expected_sha256=digest,
        editable_marker=editable,
    )
    assert bad2["ok"] is False


def test_installed_wheel_record_mismatch(tmp_path):
    wheel = _write_minimal_wheel(tmp_path / "pkg.whl", package_bytes=b"ok\n")
    digest = re.sha256_file(wheel)
    site = tmp_path / "site"
    pkg = site / "lean_weave"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_bytes(b"tampered\n")
    meta = site / "lean_weave-0.1.0.dist-info"
    meta.mkdir()
    (meta / "RECORD").write_text("x", encoding="utf-8")
    bad = re.verify_installed_wheel(
        wheel_path=wheel,
        expected_sha256=digest,
        site_packages=site,
    )
    assert bad["ok"] is False
    assert re.REASON_INSTALLED_UNVERIFIED in bad["errors"]


def test_binding_rejects_stale_lock(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes(b"stale-lock"),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    result = re.verify_artifact_binding(manifest_path=manifest, repo=repo)
    assert result["ok"] is False
    assert re.REASON_BINDING_MISMATCH in result["errors"]


def test_decide_optional_live_and_failed_blind(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    run_id = "sha256:" + "9" * 64
    re._atomic_write_json(
        evidence / "offline-receipt.json",
        {
            "schema": re.SCHEMA_OFFLINE_RECEIPT,
            "envelope": re.make_envelope(
                run_id=run_id,
                stage_id="offline",
                artifact_id=binding["artifact_id"],
                commit=commit,
                wheel_sha256=binding["wheel_sha256"],
                config_digest=None,
                dataset_digest=None,
                runner_mode="installed-wheel",
                output_hashes={"source_report": "sha256:" + "1" * 64},
                status="pass",
                reason_codes=[],
            ),
        },
    )
    re._atomic_write_json(
        evidence / "blind-receipt.json",
        {
            "schema": "lw-blind-receipt-v1",
            "envelope": re.make_envelope(
                run_id=run_id,
                stage_id="blind",
                artifact_id=binding["artifact_id"],
                commit=commit,
                wheel_sha256=binding["wheel_sha256"],
                config_digest=None,
                dataset_digest="sha256:" + "2" * 64,
                runner_mode="installed-wheel",
                output_hashes={},
                status="fail",
                reason_codes=["dependency_gate_failed"],
            ),
        },
    )
    re._atomic_write_json(
        evidence / "live-receipt.json",
        {
            "schema": re.SCHEMA_LIVE_RECEIPT,
            "state": "not_evaluated",
            "envelope": re.make_envelope(
                run_id=run_id,
                stage_id="live",
                artifact_id=binding["artifact_id"],
                commit=commit,
                wheel_sha256=binding["wheel_sha256"],
                config_digest=None,
                dataset_digest=None,
                runner_mode="live",
                output_hashes={},
                status="not_evaluated",
                reason_codes=["live_not_authorized"],
            ),
        },
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "optional"}), encoding="utf-8")
    out = tmp_path / "decisions"
    out.mkdir()
    decision, code = re.decide_release(
        repo=repo,
        commit=commit,
        evidence_root=evidence,
        policy_path=policy,
        output=out,
        manifest_path=manifest,
        expected_run_id=run_id,
    )
    assert code == 1
    assert decision["verdict"] == "Not ready"
    assert decision["gates"]["build"]["status"] == "pass"
    assert decision["gates"]["binding"]["status"] == "pass"
    assert decision["gates"]["offline"]["status"] == "pass"
    assert decision["gates"]["blind"]["status"] == "fail"
    assert decision["gates"]["live"]["status"] == "not_required"


def test_historical_baseline_not_release_schema():
    baseline = Path("benchmarks/baselines/v1-offline-baseline.json")
    assert baseline.is_file()
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload.get("schema") != re.SCHEMA_RELEASE_DECISION


def test_release_binding_env_wheel_not_formal(tmp_path, monkeypatch):
    from lean_weave.benchmark import conditions

    wheel = tmp_path / "pkg-0.1-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    monkeypatch.setenv("LEAN_WEAVE_BENCHMARK_WHEEL", str(wheel))
    monkeypatch.setattr(conditions, "code_tree_dirty", lambda: False)
    binding = conditions.release_binding()
    assert binding["wheel_sha256"].startswith("sha256:")
    assert binding["release_eligible"] is False
    assert "formal_binding_required" in binding["reasons"]
    assert "source_tree_dirty" not in binding["reasons"]


def test_build_rejects_altered_driver(tmp_path):
    repo = _init_repo(tmp_path)
    # Commit a different driver blob than the running module.
    (repo / "benchmarks" / "release_evidence.py").write_text("# altered\n", encoding="utf-8")
    _git(repo, "add", "benchmarks/release_evidence.py")
    _git(repo, "commit", "-m", "alter-driver")
    commit = _full_oid(repo)
    inputs = _build_inputs(tmp_path / "inputs.json", payload_hex=b"wheel".hex())
    out = tmp_path / "evidence"
    out.mkdir()
    code = re.main(
        [
            "build",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--output",
            str(out),
            "--build-inputs",
            str(inputs),
        ]
    )
    assert code == 1
    proof = json.loads(next(out.glob("build-*/build-proof.json")).read_text(encoding="utf-8"))
    assert "driver_bytes_mismatch" in proof["errors"]
    assert proof["matching"] is False


def test_freeze_pin_create_once(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    prereg = evidence / "prereg.json"
    prereg.write_text(
        json.dumps(
            {
                "policy_digest": "sha256:" + "a" * 64,
                "custodian": {"key_fingerprint": "sha256:" + "b" * 64, "approval_id": "a1"},
                "stages": [{"id": "offline", "tool": "pytest"}],
            }
        ),
        encoding="utf-8",
    )
    freeze_out = evidence / "runs"
    freeze_out.mkdir()
    re.freeze_run(
        manifest_path=manifest,
        preregistration_path=prereg,
        output=freeze_out,
        run_nonce="n1",
    )
    with pytest.raises(FileExistsError, match="duplicate_run_id_pin"):
        re.freeze_run(
            manifest_path=manifest,
            preregistration_path=prereg,
            output=freeze_out,
            run_nonce="n2",
        )


def test_verify_binding_rejects_mismatched_build_digests(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    proof = _matching_proof(commit, tree, wheel, evidence)
    proof["builds"][1]["wheel_sha256"] = re.sha256_bytes(b"other")
    (evidence / proof["builds"][1]["preserved_relative"]).write_bytes(b"other")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=proof,
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    # Force matching claim while digests disagree.
    binding["build_proof"]["matching"] = True
    binding["artifact_id"] = re.canonical_digest(
        {key: value for key, value in binding.items() if key != "artifact_id"}
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    result = re.verify_artifact_binding(manifest_path=manifest, repo=repo)
    assert result["ok"] is False
    assert re.REASON_BINDING_MISMATCH in result["errors"]


def test_aggregate_measured_usage_rules():
    measured = re.aggregate_measured_usage(
        [
            {"dispatched": True, "success": True, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            {"dispatched": True, "success": False, "input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
            {"no_send": True, "dispatched": False, "input_tokens": 99, "output_tokens": 99, "total_tokens": 198},
            {"estimate_only": True, "dispatched": True, "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        ]
    )
    assert measured["dispatched"] == 2
    assert measured["successes"] == 1
    assert measured["total_tokens"] == 20
    assert measured["tokens_per_success"] == 20.0
    incomplete = re.aggregate_measured_usage(
        [{"dispatched": True, "success": True, "input_tokens": 1}]
    )
    assert incomplete["coverage_complete"] is False
    assert incomplete["tokens_per_success"] is None


def test_decide_ready_path(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    run_id = "sha256:" + "7" * 64
    for name, stage in (
        ("offline-receipt.json", "offline"),
        ("blind-receipt.json", "blind"),
        ("live-receipt.json", "live"),
    ):
        payload = {
            "schema": (
                re.SCHEMA_OFFLINE_RECEIPT
                if stage == "offline"
                else ("lw-blind-receipt-v1" if stage == "blind" else re.SCHEMA_LIVE_RECEIPT)
            ),
            "state": "not_evaluated" if stage == "live" else None,
            "envelope": re.make_envelope(
                run_id=run_id,
                stage_id=stage,
                artifact_id=binding["artifact_id"],
                commit=commit,
                wheel_sha256=binding["wheel_sha256"],
                config_digest=None,
                dataset_digest=None,
                runner_mode="installed-wheel",
                output_hashes={},
                status="not_evaluated" if stage == "live" else "pass",
                reason_codes=["live_not_authorized"] if stage == "live" else [],
            ),
        }
        re._atomic_write_json(evidence / name, payload)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "optional"}), encoding="utf-8")
    out = tmp_path / "decisions"
    out.mkdir()
    decision, code = re.decide_release(
        repo=repo,
        commit=commit,
        evidence_root=evidence,
        policy_path=policy,
        output=out,
        manifest_path=manifest,
        expected_run_id=run_id,
    )
    assert code == 0
    assert decision["verdict"] == "Ready (for opt-in)"


def test_decide_required_live_missing(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    run_id = "sha256:" + "8" * 64
    for name, stage in (("offline-receipt.json", "offline"), ("blind-receipt.json", "blind")):
        re._atomic_write_json(
            evidence / name,
            {
                "schema": re.SCHEMA_OFFLINE_RECEIPT if stage == "offline" else "lw-blind-receipt-v1",
                "envelope": re.make_envelope(
                    run_id=run_id,
                    stage_id=stage,
                    artifact_id=binding["artifact_id"],
                    commit=commit,
                    wheel_sha256=binding["wheel_sha256"],
                    config_digest=None,
                    dataset_digest=None,
                    runner_mode="installed-wheel",
                    output_hashes={},
                    status="pass",
                    reason_codes=[],
                ),
            },
        )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "required"}), encoding="utf-8")
    out = tmp_path / "decisions"
    out.mkdir()
    decision, code = re.decide_release(
        repo=repo,
        commit=commit,
        evidence_root=evidence,
        policy_path=policy,
        output=out,
        manifest_path=manifest,
        expected_run_id=run_id,
    )
    assert code == 1
    assert decision["gates"]["live"]["status"] == "missing"
    assert decision["verdict"] == "Not ready"


def test_offline_required_nodes_missing(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    evidence = tmp_path / "evidence"
    artifacts = evidence / "artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "pkg.whl"
    wheel.write_bytes(b"wheel")
    binding = re.build_artifact_binding(
        evidence_root=evidence,
        proof=_matching_proof(commit, tree, wheel, evidence),
        wheel_relative="artifacts/pkg.whl",
        lock_digest=re.sha256_bytes((repo / "uv.lock").read_bytes()),
        pyproject_digest=re.sha256_bytes((repo / "pyproject.toml").read_bytes()),
        tool={},
    )
    manifest = evidence / "artifact-binding.json"
    re._atomic_write_json(manifest, binding)
    pin = evidence / "pin.json"
    pin.write_text(
        json.dumps({"run_id": "sha256:" + "1" * 64, "artifact_id": binding["artifact_id"]}),
        encoding="utf-8",
    )
    source = evidence / "source.xml"
    installed = evidence / "installed.json"
    source.write_text("<tests/>", encoding="utf-8")
    installed.write_text("{}", encoding="utf-8")
    required = evidence / "required.json"
    required.write_text(json.dumps(["tests/workflow/test_orchestrator.py::test_a"]), encoding="utf-8")
    ledger = evidence / "ledger.json"
    ledger.write_text(json.dumps({"outcomes": {}}), encoding="utf-8")
    out = evidence / "offline-out"
    code = re.main(
        [
            "offline",
            "--repo",
            str(repo),
            "--manifest",
            str(manifest),
            "--evidence-root",
            str(evidence),
            "--expected-run-id-file",
            str(pin),
            "--source-report",
            str(source),
            "--installed-report",
            str(installed),
            "--required-nodes-file",
            str(required),
            "--ledger-path",
            str(ledger),
            "--output",
            str(out),
        ]
    )
    assert code == 1
    receipt = json.loads((out / "offline-receipt.json").read_text(encoding="utf-8"))
    assert re.REASON_REQUIRED_NODE_MISSING in receipt["envelope"]["reason_codes"]


def test_decide_missing_evidence_reports_holdout_missing(tmp_path):
    repo = _init_repo(tmp_path)
    commit = _full_oid(repo)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"live": "optional"}), encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    out = tmp_path / "decisions"
    out.mkdir()
    decision, code = re.decide_release(
        repo=repo,
        commit=commit,
        evidence_root=evidence,
        policy_path=policy,
        output=out,
        manifest_path=None,
        expected_run_id=None,
    )
    assert code == 1
    assert re.REASON_HOLDOUT_MISSING in decision["unresolved_issues"]
