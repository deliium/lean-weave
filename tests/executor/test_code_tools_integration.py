import shutil
import subprocess
import sys
from pathlib import Path

from lean_weave.config.models import AppConfig, ApprovedCheck, ToolsConfig
from lean_weave.executor import apply_patch, inspect_git_diff, read_symbol, search_symbols
from lean_weave.executor.approval import approve_tools
from lean_weave.verify.checks import run_checks

REPO_FILE = Path("src/lean_weave/app.py")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _workspace_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_real_module_round_trip_stays_outside_the_checkout(tmp_path):
    original = REPO_FILE.read_bytes()
    root = tmp_path / "ws"
    destination = root / REPO_FILE
    destination.parent.mkdir(parents=True)
    shutil.copyfile(REPO_FILE, destination)
    helper = tmp_path / "parse_helper.py"
    helper.write_text(
        "import ast, pathlib, sys\nast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    _git(root, "init")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    _git(root, "add", "src/lean_weave/app.py")
    _git(root, "commit", "-m", "base")
    before_files = _workspace_files(root)

    check = ApprovedCheck(
        id="parse",
        argv=[sys.executable, str(helper), "src/lean_weave/app.py"],
        executable=sys.executable,
        timeout_s=10,
        output_bytes_max=4096,
        runs_workspace_code=False,
    )
    approval = approve_tools(
        root,
        AppConfig(tools=ToolsConfig(write_allow=["src/lean_weave/app.py"], checks=[check])),
        allow_writes=True,
        allow_checks=True,
    )
    found = search_symbols(approval, {"query": "create_context_provider"})
    assert found.ok is True
    assert any(hit.qualified_name.endswith("create_context_provider") for hit in found.hits)
    symbol = read_symbol(approval, {"path": "src/lean_weave/app.py", "name": "create_context_provider"})
    assert symbol.ok is True
    assert "def create_context_provider" in symbol.text
    envelope = symbol.model_dump_json().encode()
    assert len(envelope) < destination.stat().st_size + 2000
    assert len(envelope) <= approval.limits.result_chars_max
    assert len(symbol.text) < len(destination.read_text(encoding="utf-8"))

    lines = destination.read_text(encoding="utf-8").splitlines()
    index = next(number for number, line in enumerate(lines) if "no scan or Git here." in line)
    old = lines[index]
    new = old.replace("no scan or Git here.", "no scan or Git here. probe")
    diff = (
        "--- a/src/lean_weave/app.py\n"
        "+++ b/src/lean_weave/app.py\n"
        f"@@ -{index + 1},1 +{index + 1},1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )
    applied = apply_patch(
        approval,
        {"path": "src/lean_weave/app.py", "expected_revision": symbol.revision, "diff": diff},
    )
    assert applied.ok is True
    assert "probe" in destination.read_text(encoding="utf-8")
    stale = apply_patch(
        approval,
        {"path": "src/lean_weave/app.py", "expected_revision": symbol.revision, "diff": diff},
    )
    assert stale.status == "stale"
    viewed = inspect_git_diff(approval, {"mode": "unstaged", "paths": ["src/lean_weave/app.py"]})
    assert viewed.status == "changes"
    assert viewed.entries[0].path == "src/lean_weave/app.py"
    checked = run_checks(approval, {"check_ids": ["parse"]})
    assert checked.ok is True
    assert checked.items[0].status == "passed"
    after_files = _workspace_files(root)
    assert after_files.keys() == before_files.keys()
    assert [path for path in after_files if after_files[path] != before_files[path]] == ["src/lean_weave/app.py"]
    assert REPO_FILE.read_bytes() == original
