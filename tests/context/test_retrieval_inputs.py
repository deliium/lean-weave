"""Contracts for offline retrieval config, requests, and strict text admission."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from lean_weave.config import AppConfig, ContextConfig, RetrievalConfig, RetrievalWeights
from lean_weave.config.loader import load_config
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import (
    DEGRADED_REASON_ORDER,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResult,
    SignalReason,
)
from lean_weave.errors import InvalidContextRequest

from .conftest import write_files
from .test_repository import strict_repository


def test_retrieval_config_defaults_are_backward_compatible():
    context = AppConfig().context
    assert context.retrieval.top_k == 10
    weights = context.retrieval.weights
    assert weights.exact_symbol == 36
    assert weights.name == 24
    assert weights.lexical == 24
    assert weights.import_dependency == 8
    assert weights.reverse_dependency == 8
    assert weights.git_diff == 5
    assert weights.related_test == 8
    assert weights.map_importance == 3
    assert ContextConfig().retrieval == RetrievalConfig()


def test_retrieval_weight_partial_override(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("context:\n  retrieval:\n    top_k: 7\n    weights:\n      lexical: 10\n")
    loaded = load_config(path, required=True)
    assert loaded.config.context.retrieval.top_k == 7
    assert loaded.config.context.retrieval.weights.lexical == 10
    assert loaded.config.context.retrieval.weights.exact_symbol == 36


@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"retrieval": {"top_k": True}}},
        {"context": {"retrieval": {"top_k": 0}}},
        {"context": {"retrieval": {"top_k": 101}}},
        {"context": {"retrieval": {"top_k": -1}}},
        {"context": {"retrieval": {"unknown": 1}}},
        {"context": {"retrieval": {"weights": {"exact_symbol": -1}}}},
        {"context": {"retrieval": {"weights": {"exact_symbol": 101}}}},
        {"context": {"retrieval": {"weights": {"exact_symbol": True}}}},
        {"context": {"retrieval": {"weights": {"bonus": 1}}}},
        {
            "context": {
                "retrieval": {
                    "weights": {
                        "exact_symbol": 0,
                        "name": 0,
                        "lexical": 0,
                    }
                }
            }
        },
        {
            "context": {
                "retrieval": {
                    "weights": {
                        "exact_symbol": 0,
                        "name": 0,
                        "lexical": 0,
                        "git_diff": 50,
                        "map_importance": 50,
                    }
                }
            }
        },
    ],
)
def test_retrieval_config_rejects_strict_invalid(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_retrieval_request_strips_and_accepts_defaults():
    request = RetrievalRequest(query="  fix login bug  ")
    assert request.query == "fix login bug"
    assert request.task_kind == "feature"
    assert request.kind == "all"
    assert request.top_k is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "   "},
        {"query": ""},
        {"query": "a" * 2001},
        {"query": "bad\nline"},
        {"query": "has\x00null"},
        {"query": "tab\there"},
        {"query": "api_key='abcdefghijklmnopqrstuvwxyz012345'"},
        {"query": "ok", "task_kind": "unknown"},
        {"query": "ok", "kind": "snippet"},
        {"query": "ok", "top_k": 0},
        {"query": "ok", "top_k": 101},
        {"query": "ok", "top_k": True},
    ],
)
def test_retrieval_request_rejects_invalid(kwargs):
    with pytest.raises((InvalidContextRequest, ValidationError)):
        RetrievalRequest(**kwargs)


def test_retrieval_request_credential_error_is_generic():
    secret = "api_key='abcdefghijklmnopqrstuvwxyz012345'"
    with pytest.raises(InvalidContextRequest) as error:
        RetrievalRequest(query=secret)
    assert secret not in str(error.value)
    assert "abcdefghijklmnopqrstuvwxyz012345" not in str(error.value)


def test_retrieval_request_top_k_endpoints():
    assert RetrievalRequest(query="x", top_k=1).top_k == 1
    assert RetrievalRequest(query="x", top_k=100).top_k == 100


def test_retrieval_hit_and_result_contracts():
    hit = RetrievalHit(
        kind="file",
        path="a.py",
        start_line=1,
        end_line=2,
        source_digest="sha256:x",
        source_kind="file_head",
        relevance_score=10,
        ranking_reasons=(
            SignalReason(signal="name", value=1, weight=24, weighted_points=24),
        ),
        estimated_token_cost=3,
    )
    empty = RetrievalHit(
        kind="file",
        path="empty.py",
        source_digest="sha256:y",
        source_kind="empty_file",
        relevance_score=0,
        estimated_token_cost=0,
    )
    result = RetrievalResult(
        hits=(hit, empty),
        task_kind="bugfix",
        kind="all",
        top_k=10,
        degraded_reasons=DEGRADED_REASON_ORDER[:2],
        truncated=True,
    )
    assert result.truncated is True
    with pytest.raises(ValidationError):
        RetrievalResult(
            hits=(),
            task_kind="feature",
            kind="all",
            top_k=10,
            degraded_reasons=("git_unavailable", "index_missing"),
        )


def test_retrieval_scan_admits_supported_text_and_python_differs(project: Path):
    write_files(
        project,
        {
            "pkg/mod.py": "x = 1\n",
            "README.md": "# docs\n",
            "notes.txt": "hello\n",
            "binary.bin": b"\x00\x01",
            "skip.pyc": b"x",
        },
    )
    with strict_repository(project).scan_python_sources() as python_scan:
        assert [item.path for item in python_scan.snapshots] == ["pkg/mod.py"]
        assert python_scan.policy.mode == "python"
        assert python_scan.complete is True

    with strict_repository(project).scan_retrieval_sources() as text_scan:
        assert [item.path for item in text_scan.snapshots] == [
            "README.md",
            "notes.txt",
            "pkg/mod.py",
        ]
        assert text_scan.policy.mode == "retrieval"
        assert text_scan.complete is True


def test_retrieval_scan_freezes_policy_after_open(project: Path):
    write_files(project, {"keep.py": "x = 1\n", "drop.md": "n\n"})
    config = ContextConfig(exclusions=["drop.md"], candidate_bytes_max=4096)
    repository = Repository(project, config, strict_source=True)
    with repository.scan_retrieval_sources() as scan:
        frozen = scan.policy
        assert frozen.exclusions == ("drop.md",)
        assert frozen.candidate_bytes_max == 4096
        config.exclusions.append("keep.py")
        config.candidate_bytes_max = 1
        repository.config.exclusions.append("keep.py")
        assert scan.policy == frozen
        assert [item.path for item in scan.snapshots] == ["keep.py"]
        assert scan.policy.exclusions == ("drop.md",)
        assert scan.policy.candidate_bytes_max == 4096


def test_retrieval_scan_respects_nested_ignore_symlink_protection(project: Path):
    write_files(
        project,
        {
            ".gitignore": "ignored/*\n",
            "ok.py": "x = 1\n",
            "ok.md": "docs\n",
            "ignored/secret.md": "nope\n",
            "config.yaml": "a: 1\n",
        },
    )
    (project / "link.md").symlink_to(project / "ok.md")
    repository = Repository(
        project,
        ContextConfig(),
        strict_source=True,
        protected_paths=("config.yaml",),
    )
    with repository.scan_retrieval_sources() as scan:
        assert scan.complete is True
        assert [item.path for item in scan.snapshots] == ["ok.md", "ok.py"]


def test_retrieval_scan_quarantines_credentials_without_echo(project: Path):
    secret = "sk-" + "a" * 32
    write_files(
        project,
        {
            "leak.md": f"token {secret}\n",
            "ok.py": "x = 1\n",
        },
    )
    with strict_repository(project).scan_retrieval_sources() as scan:
        assert [item.path for item in scan.snapshots] == ["ok.py"]
        assert scan.omission_counts.get("secret") == 1
        assert secret not in repr(scan)


def test_retrieval_scan_marks_incomplete_on_visit_budget(project: Path):
    write_files(project, {"a.py": "x = 1\n", "b.md": "y\n"})
    with strict_repository(project, visited_entries_max=1).scan_retrieval_sources() as scan:
        assert scan.complete is False
        assert "scan_limit" in scan.omission_counts


def test_retrieval_scan_rejects_hardlinks(project: Path):
    write_files(project, {"one.md": "x\n"})
    os.link(project / "one.md", project / "two.md")
    with strict_repository(project).scan_retrieval_sources() as scan:
        assert scan.complete is True
        assert scan.snapshots == ()


def test_python_mode_still_ignores_markdown(project: Path):
    write_files(project, {"a.py": "x = 1\n", "a.md": "docs\n"})
    with strict_repository(project).scan_python_sources() as scan:
        assert [item.path for item in scan.snapshots] == ["a.py"]
        assert scan.policy.mode == "python"
