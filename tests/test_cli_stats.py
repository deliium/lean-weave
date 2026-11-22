from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx2
from typer.testing import CliRunner

from lean_weave.benchmark.offline import (
    ContentShadow,
    ScriptedTransport,
    prepare_case,
    success_script,
)
from lean_weave.cli.app import app
from lean_weave.workflow.session import FeatureSession


def _run_feature(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    case = prepare_case("bug_fix", tmp_path / "case")
    shadow = ContentShadow.from_tree(case.origin)
    transport = ScriptedTransport(success_script(case.manifest, shadow))
    session = FeatureSession(source_cwd=case.origin)
    view = session.start(
        case.manifest.request,
        mode="auto",
        config_path=case.config_path,
        transport=httpx2.MockTransport(transport.handler),
    )
    assert view.status == "success"
    return case, session, view


def test_stats_feature_and_inventory_json(tmp_path, monkeypatch):
    case, session, view = _run_feature(tmp_path, monkeypatch)
    monkeypatch.chdir(case.origin)
    runner = CliRunner()
    feature = runner.invoke(app, ["stats", "feature", view.feature_id, "--json"])
    assert feature.exit_code == 0, feature.stdout
    payload = json.loads(feature.stdout.strip().splitlines()[-1])
    assert payload["feature_id"] == view.feature_id
    assert payload["available"] is True
    assert payload["usage"]["totals"]["total_tokens"] == 42
    assert payload["workflow_retry_count"] == 0
    inventory = runner.invoke(app, ["stats", "--json"])
    assert inventory.exit_code == 0, inventory.stdout
    inventory_payload = json.loads(inventory.stdout.strip().splitlines()[-1])
    assert [item["feature_id"] for item in inventory_payload["features"]] == [
        view.feature_id
    ]
    assert inventory_payload["partial"] is False


def test_stats_after_archive_matches_live(tmp_path, monkeypatch):
    case, session, view = _run_feature(tmp_path, monkeypatch)
    live = session.stats(view.feature_id)
    worktree = Path(session.inspect_worktree(view.feature_id)["worktree_root"])
    subprocess.run(
        ["git", "-C", str(worktree), "add", "--", "src/calc.py"], check=True
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-qm", "fixture change"],
        check=True,
    )
    session.cleanup_worktree(view.feature_id, confirm=True)
    archived = session.stats(view.feature_id)
    assert archived.available is True
    assert archived.archived is True
    assert archived.usage["totals"]["total_tokens"] == live.usage["totals"]["total_tokens"]


def test_stats_unknown_feature_fails_cleanly(tmp_path, monkeypatch):
    case, _session, _view = _run_feature(tmp_path, monkeypatch)
    monkeypatch.chdir(case.origin)
    result = CliRunner().invoke(app, ["stats", "feature", "missing", "--json"])
    assert result.exit_code != 0
    assert "feature" not in result.stdout.lower() or "Traceback" not in result.stdout
