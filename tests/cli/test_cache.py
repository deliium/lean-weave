from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lean_weave.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path.parent / f"{tmp_path.name}-cache"
    cache.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "use.py").write_text(
        "from pkg.core import helper\n\ndef use():\n    return helper()\n",
        encoding="utf-8",
    )
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def _enable(root: Path) -> None:
    (root / ".lean-weave").mkdir()
    (root / ".lean-weave" / "config.yaml").write_text(
        "context:\n  cache:\n    enabled: true\n", encoding="utf-8"
    )


def test_cache_help_is_offline_even_with_invalid_config(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("llm: [", encoding="utf-8")
    result = runner.invoke(app, ["--config", "bad.yaml", "cache", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "Config.load" not in result.stderr


def test_disabled_status_stats_and_rejected_rebuild(isolated: Path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("cache commands must not construct an LLM provider")

    monkeypatch.setattr("lean_weave.app.create_llm_provider", fail)
    status = invoke("cache", "status")
    assert status.exit_code == 0
    assert "enabled=false health=disabled" in status.stdout
    stats = invoke("cache", "stats")
    assert stats.exit_code == 0
    assert "hit=0 miss=0" in stats.stdout
    rebuild = invoke("cache", "rebuild")
    assert rebuild.exit_code == 2
    assert "disabled" in rebuild.stderr
    clear = invoke("cache", "clear")
    assert clear.exit_code == 2
    assert "requires --yes" in clear.stderr


def test_rebuild_status_stats_clear(isolated: Path, monkeypatch):
    _enable(isolated)
    monkeypatch.setattr("lean_weave.app.create_llm_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")))
    assert invoke("index", "build").exit_code == 0
    rebuilt = invoke("cache", "rebuild")
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "health=ready" in rebuilt.stdout
    assert "facts=" in rebuilt.stdout and "spans=" in rebuilt.stdout
    assert "map_seed=ready" in rebuilt.stdout

    status = invoke("cache", "status")
    assert status.exit_code == 0
    assert "enabled=true health=ready" in status.stdout
    assert "entries=" in status.stdout and "schema=2" in status.stdout

    stats = invoke("cache", "stats")
    assert stats.exit_code == 0
    assert "rebuild=" in stats.stdout
    assert "kind facts" in stats.stdout

    cleared = invoke("cache", "clear", "--yes")
    assert cleared.exit_code == 0
    assert cleared.stdout.strip() == "cleared"
    again = invoke("cache", "clear", "--yes")
    assert again.exit_code == 0
    assert again.stdout.strip() == "missing"

    missing = invoke("cache", "status")
    assert missing.exit_code == 0
    assert "health=missing" in missing.stdout


def test_clear_works_after_disabling(isolated: Path):
    _enable(isolated)
    assert invoke("cache", "rebuild").exit_code == 0
    (isolated / ".lean-weave" / "config.yaml").write_text(
        "context:\n  cache:\n    enabled: false\n", encoding="utf-8"
    )
    cleared = invoke("cache", "clear", "--yes")
    assert cleared.exit_code == 0
    assert cleared.stdout.strip() == "cleared"


def test_rebuilt_cache_serves_facts_and_spans(isolated: Path):
    _enable(isolated)
    assert invoke("index", "build").exit_code == 0
    assert invoke("cache", "rebuild").exit_code == 0

    from lean_weave.app import build_app_context, create_context_cache
    from lean_weave.context.packing_source import _python_spans
    from lean_weave.context.repository import Repository
    from lean_weave.context_cache import ArtifactKind
    from lean_weave.context_cache.facts import CachedFactLoader
    from lean_weave.context_cache.packing import PackingSpanCache

    context = build_app_context()
    cache = create_context_cache(context, workspace_root=isolated)
    repository = Repository(
        isolated, context.config.context, strict_source=True
    )
    snapshot = repository.read("pkg/core.py")
    before = cache.stats().count("hit", ArtifactKind.FACTS)
    with CachedFactLoader(cache, repository, policy=cache.policy) as load:
        load(snapshot)
    assert cache.stats().count("hit", ArtifactKind.FACTS) > before

    span_cache = PackingSpanCache(cache, policy=cache.policy)
    before_spans = cache.stats().count("hit", ArtifactKind.PACKING_SPANS)
    span_cache.load(
        snapshot,
        batch=cache.begin_batch(),
        compute=lambda: _python_spans(snapshot),
    )
    assert cache.stats().count("hit", ArtifactKind.PACKING_SPANS) > before_spans


def test_rebuild_without_index_skips_map_seed(isolated: Path):
    _enable(isolated)
    rebuilt = invoke("cache", "rebuild")
    assert rebuilt.exit_code == 0
    assert "map_seed=skipped" in rebuilt.stdout
    assert "facts=" in rebuilt.stdout


def test_rebuild_reads_index_before_entering_cache_lease(
    isolated: Path, monkeypatch
):
    _enable(isolated)
    assert invoke("index", "build").exit_code == 0

    from lean_weave.app import build_app_context
    from lean_weave.code_index import CodeIndex
    from lean_weave.context_cache.maintenance import rebuild_context_cache
    from lean_weave.context_cache.service import SqliteContextCache

    events: list[str] = []
    original_lease = SqliteContextCache._exclusive_lease
    original_read = CodeIndex.read_generation_for_retrieval_scan

    @contextlib.contextmanager
    def tracking_lease(self):
        events.append("lease_enter")
        try:
            with original_lease(self) as fd:
                yield fd
        finally:
            events.append("lease_exit")

    def tracking_read(self, scan):
        events.append("index_read")
        return original_read(self, scan)

    monkeypatch.setattr(SqliteContextCache, "_exclusive_lease", tracking_lease)
    monkeypatch.setattr(
        CodeIndex, "read_generation_for_retrieval_scan", tracking_read
    )
    context = build_app_context()
    rebuild_context_cache(context, workspace_root=isolated)
    assert "index_read" in events
    assert events.index("index_read") < events.index("lease_enter")


def test_corrupt_cache_reports_health_and_hint(isolated: Path):
    _enable(isolated)
    assert invoke("cache", "rebuild").exit_code == 0
    database = next((isolated.parent / f"{isolated.name}-cache").rglob("cache.sqlite3"))
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=99")
    connection.commit()
    connection.close()
    broken = invoke("cache", "status")
    assert broken.exit_code == 1
    assert "health=incompatible" in broken.stdout
    assert "hint:" in broken.stderr
