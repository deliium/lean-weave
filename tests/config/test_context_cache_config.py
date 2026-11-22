from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lean_weave.config import load_config
from lean_weave.config.models import AppConfig, ContextCacheConfig


def test_cache_defaults_are_disabled():
    config = AppConfig()
    assert config.context.cache.enabled is False
    assert config.context.cache.max_bytes == 16_777_216
    assert config.context.cache.max_entries == 4096


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_bytes", 1_048_575),
        ("max_bytes", 67_108_865),
        ("max_entries", 0),
        ("max_entries", 16_385),
    ],
)
def test_cache_limits_are_strict(field: str, value: int):
    with pytest.raises(ValidationError):
        ContextCacheConfig(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["path", "cross_worktree", "auto_rebuild", "peer", "shared"],
)
def test_unknown_authorization_fields_are_rejected(field: str):
    with pytest.raises(ValidationError):
        ContextCacheConfig(**{field: True})


def test_existing_config_without_cache_still_loads(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lean-weave").mkdir()
    (tmp_path / ".lean-weave" / "config.yaml").write_text(
        "workflow:\n  state_dir: .lean-weave\n", encoding="utf-8"
    )
    loaded = load_config()
    assert loaded.config.context.cache.enabled is False


def test_enabled_cache_loads(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lean-weave").mkdir()
    (tmp_path / ".lean-weave" / "config.yaml").write_text(
        "context:\n  cache:\n    enabled: true\n    max_bytes: 2097152\n",
        encoding="utf-8",
    )
    loaded = load_config()
    assert loaded.config.context.cache.enabled is True
    assert loaded.config.context.cache.max_bytes == 2_097_152
