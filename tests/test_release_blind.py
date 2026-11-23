"""Synthetic blind capture/score protocol tests (no real holdout)."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from benchmarks import release_blind_capture as capture
from benchmarks import release_blind_score as score


def _suite(n_cases: int = 12, heavy: int = 8, repos: int = 3) -> dict:
    cases = []
    for index in range(n_cases):
        repo = f"repo-{index % repos}"
        item = {
            "id": f"case-{index:02d}",
            "repository": repo,
            "dependency_heavy": index < heavy,
            "relevant_files": [f"{repo}/a.py", f"{repo}/b.py"],
            "relevant_symbols": [],
            "required_dependencies": [
                {"path": f"{repo}/a.py", "start_line": 1, "end_line": 2},
                {"path": f"{repo}/b.py", "start_line": 1, "end_line": 3},
            ]
            if index < heavy
            else [{"path": f"{repo}/a.py", "start_line": 1, "end_line": 1}],
            "edit_anchor": {"path": f"{repo}/a.py", "start_line": 1, "end_line": 1},
        }
        cases.append(item)
    return {"schema": score.SCHEMA_FREEZE, "cases": cases}


def test_validate_suite_thresholds():
    assert score.validate_suite(_suite()) == []
    assert "insufficient_cases" in score.validate_suite(_suite(n_cases=5))
    assert "insufficient_repositories" in score.validate_suite(_suite(repos=2))
    assert "insufficient_dependency_heavy" in score.validate_suite(_suite(heavy=3))
    empty = _suite()
    empty["cases"][0]["relevant_files"] = []
    empty["cases"][0]["required_dependencies"] = []
    empty["cases"][0]["dependency_heavy"] = False
    assert "empty_denominator" in score.validate_suite(empty)


def test_capture_rejects_oracle_keys(tmp_path):
    spec = {
        "case_id": "c1",
        "request": "edit helper",
        "edit_target": "pkg/a.py",
        "oracle": {"required_dependencies": []},
        "arms": [],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="oracle_leak"):
        capture.run_synthetic_capture(path, tmp_path / "out.json")


def test_capture_and_score_pass(tmp_path):
    source_a = "class Anchor:\n    pass\n"
    source_b = "def helper():\n    return 1\n"
    spec = {
        "case_id": "c1",
        "request": "extend Anchor",
        "edit_target": "pkg/a.py",
        "fixture_digest": "sha256:" + "a" * 64,
        "arms": [
            {
                "arm": "v1",
                "status": "ok",
                "setup_ms": 1.0,
                "materialize_ms": 2.0,
                "estimated_input_tokens": 10,
                "delivered_units": [
                    {"path": "pkg/a.py", "start_line": 1, "end_line": 2, "text": source_a},
                    {"path": "pkg/b.py", "start_line": 1, "end_line": 2, "text": source_b},
                ],
            },
            {
                "arm": "v2",
                "status": "ok",
                "setup_ms": 1.5,
                "materialize_ms": 2.5,
                "estimated_input_tokens": 12,
                "delivered_units": [
                    {"path": "pkg/a.py", "start_line": 1, "end_line": 2, "text": source_a},
                    {"path": "pkg/b.py", "start_line": 1, "end_line": 2, "text": source_b},
                ],
            },
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    capture_path = tmp_path / "capture.json"
    payload = capture.run_synthetic_capture(spec_path, capture_path)
    assert payload["records"]
    assert "oracle" not in json.dumps(payload)

    labels = {
        "relevant_files": ["pkg/a.py", "pkg/b.py"],
        "required_dependencies": [
            {"path": "pkg/a.py", "start_line": 1, "end_line": 2},
            {"path": "pkg/b.py", "start_line": 1, "end_line": 2},
        ],
        "edit_anchor": {"path": "pkg/a.py", "start_line": 1, "end_line": 1},
    }
    scored = score.score_pair(capture=payload, labels=labels)
    assert scored["ok"] is True
    assert scored["gates"]["v2_zero_required_misses"] is True


def test_score_detects_dependency_gap_and_incomplete_pair(tmp_path):
    capture_payload = {
        "case_id": "c2",
        "records": [
            {
                "arm": "v1",
                "status": "ok",
                "spans": [
                    {
                        "path": "pkg/a.py",
                        "start_line": 1,
                        "end_line": 2,
                        "text_digest": "sha256:" + "1" * 64,
                        "byte_len": 10,
                    }
                ],
                "context_bytes": 10,
            },
            {
                "arm": "v2",
                "status": "ok",
                "spans": [
                    {
                        "path": "pkg/a.py",
                        "start_line": 1,
                        "end_line": 1,
                        "text_digest": "sha256:" + "1" * 64,
                        "byte_len": 4,
                    }
                ],
                "context_bytes": 4,
            },
        ],
    }
    labels = {
        "relevant_files": ["pkg/a.py"],
        "required_dependencies": [
            {"path": "pkg/a.py", "start_line": 1, "end_line": 2},
        ],
        "edit_anchor": {"path": "pkg/a.py", "start_line": 1, "end_line": 1},
    }
    scored = score.score_pair(capture=capture_payload, labels=labels)
    assert scored["ok"] is False
    assert "dependency_gate_failed" in scored["reason_codes"]

    incomplete = {
        "case_id": "c3",
        "records": [capture_payload["records"][0]],
    }
    scored2 = score.score_pair(capture=incomplete, labels=labels)
    assert "pair_incomplete" in scored2["reason_codes"]


def test_union_spans_and_map_only_negative():
    spans = [
        {"path": "a.py", "start_line": 1, "end_line": 2, "text_digest": "d"},
        {"path": "a.py", "start_line": 3, "end_line": 4, "text_digest": "d"},
    ]
    merged = score.union_spans(spans)
    assert merged == [
        {"path": "a.py", "start_line": 1, "end_line": 4, "text_digest": "d"}
    ]
    # Map-only "coverage" without delivered spans does not satisfy intervals.
    assert (
        score.interval_covered([], path="a.py", start_line=1, end_line=1) is False
    )


def test_blind_receipt_create_once(tmp_path):
    capture_path = tmp_path / "capture.json"
    labels_path = tmp_path / "labels.json"
    capture_path.write_text(
        json.dumps(
            {
                "case_id": "c",
                "records": [
                    {
                        "arm": "v1",
                        "status": "ok",
                        "spans": [
                            {
                                "path": "a.py",
                                "start_line": 1,
                                "end_line": 1,
                                "text_digest": "sha256:" + "1" * 64,
                                "byte_len": 1,
                            }
                        ],
                    },
                    {
                        "arm": "v2",
                        "status": "ok",
                        "spans": [
                            {
                                "path": "a.py",
                                "start_line": 1,
                                "end_line": 1,
                                "text_digest": "sha256:" + "1" * 64,
                                "byte_len": 1,
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    labels_path.write_text(
        json.dumps(
            {
                "relevant_files": ["a.py"],
                "required_dependencies": [
                    {"path": "a.py", "start_line": 1, "end_line": 1}
                ],
                "edit_anchor": {"path": "a.py", "start_line": 1, "end_line": 1},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "blind-receipt.json"
    args = [
        "score",
        "--capture",
        str(capture_path),
        "--labels",
        str(labels_path),
        "--output",
        str(out),
        "--run-id",
        "sha256:" + "a" * 64,
        "--artifact-id",
        "sha256:" + "b" * 64,
        "--suite-digest",
        "sha256:" + "c" * 64,
        "--schedule-digest",
        "sha256:" + "e" * 64,
    ]
    assert score.main(args) == 0
    assert out.is_file()
    # Duplicate output path must fail closed.
    assert score.main(args) == 1


def test_installed_capture_subprocess_stub(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.py").write_text("def anchor():\n    return 1\n", encoding="utf-8")
    stub = tmp_path / "stub_worker.py"
    stub.write_text(
        textwrap.dedent(
            """\
            import json, sys
            from pathlib import Path
            spec = json.loads(Path(sys.argv[1]).read_text())
            out = Path(sys.argv[2])
            text = "def anchor():\\n    return 1\\n"
            out.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "setup_ms": 1.0,
                        "materialize_ms": 2.0,
                        "delivered_units": [
                            {
                                "path": "a.py",
                                "start_line": 1,
                                "end_line": 2,
                                "text": text,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            """
        ),
        encoding="utf-8",
    )
    spec = {
        "case_id": "installed-1",
        "request": "edit anchor",
        "edit_target": "a.py",
        "fixture_root": str(fixture),
        "fixture_files": ["a.py"],
        "arms": [
            {"arm": "v1", "worker_stub": str(stub)},
            {"arm": "v2", "worker_stub": str(stub)},
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "capture.json"
    python = shutil.which("python3") or "python3"
    payload = capture.run_installed_capture(spec_path, out, python=python)
    assert payload["runtime_mode"] == "installed-wheel"
    assert len(payload["records"]) == 2
    assert payload["fixture_digest_before"] == payload["fixture_digest_after"]


def test_symbol_recall_gate():
    source = "def helper():\n    return 1\n"
    capture_payload = {
        "case_id": "sym",
        "records": [
            {
                "arm": "v1",
                "status": "ok",
                "spans": [
                    {
                        "path": "a.py",
                        "start_line": 1,
                        "end_line": 2,
                        "text_digest": capture.sha256_bytes(source.encode("utf-8")),
                        "byte_len": len(source),
                    }
                ],
                "symbol_ids": [["a.py", "helper", 1]],
                "context_bytes": len(source),
                "timing_ms": {"total": 3.0},
            },
            {
                "arm": "v2",
                "status": "ok",
                "spans": [
                    {
                        "path": "a.py",
                        "start_line": 2,
                        "end_line": 2,
                        "text_digest": capture.sha256_bytes(b"    return 1\n"),
                        "byte_len": 4,
                    }
                ],
                "symbol_ids": [],
                "context_bytes": 4,
                "timing_ms": {"total": 4.0},
            },
        ],
    }
    labels = {
        "relevant_files": ["a.py"],
        "relevant_symbols": [
            {"path": "a.py", "qualified_name": "helper", "start_line": 1}
        ],
        "required_dependencies": [
            {"path": "a.py", "start_line": 1, "end_line": 1}
        ],
        "edit_anchor": {"path": "a.py", "start_line": 1, "end_line": 1},
    }
    scored = score.score_pair(capture=capture_payload, labels=labels)
    assert scored["ok"] is False
    assert "recall_gate_failed" in scored["reason_codes"]
    assert scored["latency"]["count"] == 2


def test_retire_corpus_create_once(tmp_path):
    marker = tmp_path / "retired.json"
    assert score.main(["retire-corpus", "--marker", str(marker)]) == 0
    assert json.loads(marker.read_text(encoding="utf-8"))["retired"] is True
    assert score.main(["retire-corpus", "--marker", str(marker)]) == 1


def test_verify_signature_cli(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl unavailable")
    message = tmp_path / "message.bin"
    message.write_bytes(b"blind-receipt")
    # Generate a throwaway Ed25519 keypair when supported.
    key = tmp_path / "key.pem"
    pub = tmp_path / "pub.pem"
    sig = tmp_path / "msg.sig"
    gen = __import__("subprocess").run(
        [openssl, "genpkey", "-algorithm", "Ed25519", "-out", str(key)],
        check=False,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
    )
    if gen.returncode != 0:
        pytest.skip("openssl Ed25519 unavailable")
    __import__("subprocess").run(
        [openssl, "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
    )
    signed = __import__("subprocess").run(
        [
            openssl,
            "pkeyutl",
            "-sign",
            "-inkey",
            str(key),
            "-rawin",
            "-in",
            str(message),
            "-out",
            str(sig),
        ],
        check=False,
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
    )
    if signed.returncode != 0:
        pytest.skip("openssl rawin sign unavailable")
    assert (
        score.main(
            [
                "verify-signature",
                "--openssl",
                openssl,
                "--public-key",
                str(pub),
                "--message",
                str(message),
                "--signature",
                str(sig),
            ]
        )
        == 0
    )
    sig.write_bytes(b"bad")
    assert (
        score.main(
            [
                "verify-signature",
                "--openssl",
                openssl,
                "--public-key",
                str(pub),
                "--message",
                str(message),
                "--signature",
                str(sig),
            ]
        )
        == 1
    )
