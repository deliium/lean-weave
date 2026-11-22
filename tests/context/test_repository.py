import os
import stat as stat_module
from pathlib import Path
from types import SimpleNamespace

import pytest

import lean_weave.context.repository as repository_module
from lean_weave.config import ConfigError, ContextConfig
from lean_weave.context.models import OmissionReason
from lean_weave.context.repository import Repository, validate_index_source_path
from lean_weave.errors import ForbiddenContext, InvalidContextRequest

from .conftest import init_git, write_files


@pytest.fixture
def repository_root(project: Path) -> Path:
    write_files(
        project,
        {
            ".gitignore": "ignored/*\n!ignored/keep.py\n*.log\n",
            "keep.py": "VALUE = 1\n",
            "ignored/keep.py": "KEEP = 1\n",
            "ignored/drop.py": "DROP = 1\n",
            "drop.log": "noise\n",
            ".env": "SECRET=1\n",
            ".lean-weave/state.json": "{}\n",
            ".ai-factory/plans/x.md": "plan\n",
            ".opencode/skills/x.py": "skill\n",
            "node_modules/x.js": "x\n",
            "dist/x.js": "x\n",
            "pkg/__init__.py": "",
            "pkg/mod.py": "def f():\n    return 1\n",
            "binary.txt": b"\x00\x01\x02",
            "token_leak.py": "PRIVATE = '-----BEGIN RSA PRIVATE KEY-----'\n",
        },
    )
    return project


def make_repository(root: Path, **overrides) -> Repository:
    return Repository(root, ContextConfig(**overrides), state_dir=".lean-weave")


def test_inventory_applies_ignore_and_hard_denials(repository_root: Path):
    repository = make_repository(repository_root)
    files = repository.inventory().files
    assert "keep.py" in files
    assert "ignored/keep.py" in files
    assert "ignored/drop.py" not in files
    assert "drop.log" not in files
    assert "pkg/mod.py" in files
    for denied in (
        ".env",
        ".lean-weave/state.json",
        ".ai-factory/plans/x.md",
        ".opencode/skills/x.py",
        "node_modules/x.js",
        "dist/x.js",
    ):
        assert denied not in files


def test_secret_like_path_names_are_not_exposed(project: Path):
    secret_name = "sk-" + "c" * 32 + ".py"
    write_files(project, {secret_name: "VALUE = 1\n", "keep.py": "VALUE = 1\n"})
    repository = make_repository(project)
    assert secret_name not in repository.inventory().files
    assert all(secret_name not in (item.path or "") for item in repository.omissions())


def test_configured_exclusions_are_additive_and_not_negated(repository_root: Path):
    write_files(
        repository_root,
        {
            ".gitignore": "pkg/*\n!pkg/mod.py\nignored/*\n",
        },
    )
    repository = make_repository(repository_root, exclusions=["pkg/"])
    files = repository.inventory().files
    assert "pkg/mod.py" not in files


def test_secret_content_is_quarantined_without_echo(repository_root: Path):
    repository = make_repository(repository_root)
    assert repository.try_read("token_leak.py") is None
    text = str(repository.omissions())
    assert "BEGIN RSA PRIVATE KEY" not in text
    assert "token_leak.py" not in text
    assert any(item.reason == OmissionReason.SECRET for item in repository.omissions())
    with pytest.raises(ForbiddenContext):
        repository.read("token_leak.py")


def test_binary_oversized_and_symlink(repository_root: Path, tmp_path: Path):
    (repository_root / "big.txt").write_text("x" * 64)
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("outside\n")
    (repository_root / "link.txt").symlink_to(outside)
    repository = make_repository(repository_root, candidate_bytes_max=32)
    assert repository.try_read("binary.txt") is None
    assert repository.try_read("big.txt") is None
    assert any(item.reason == OmissionReason.BINARY for item in repository.omissions())
    assert any(item.reason == OmissionReason.OVERSIZED for item in repository.omissions())
    assert not repository.is_eligible("link.txt")
    with pytest.raises(ForbiddenContext):
        repository.read("link.txt")


def test_scan_limit_is_bounded(project: Path):
    write_files(project, {f"f{index}.py": "x = 1\n" for index in range(10)})
    repository = make_repository(project, visited_entries_max=3)
    inventory = repository.inventory()
    assert inventory.complete is False
    assert len(inventory.files) <= 3
    assert any(item.reason == OmissionReason.SCAN_LIMIT for item in inventory.omissions)


def test_state_dir_must_not_be_root(project: Path):
    with pytest.raises(ConfigError, match="state_dir"):
        Repository(project, ContextConfig(), state_dir=".")


def test_nested_repository_is_not_traversed(project: Path):
    write_files(
        project,
        {
            "outer.py": "x = 1\n",
            "nested/.git/HEAD": "ref: refs/heads/main\n",
            "nested/inner.py": "y = 1\n",
        },
    )
    files = make_repository(project).inventory().files
    assert "outer.py" in files
    assert "nested/inner.py" not in files


def test_git_presence_does_not_change_eligibility(project: Path):
    write_files(project, {"pkg/mod.py": "x = 1\n", "README.md": "hello\n"})
    before = make_repository(project).inventory().files
    init_git(project)
    after = make_repository(project).inventory().files
    assert before == after


def test_containment_is_rechecked_on_read(project: Path):
    write_files(project, {"pkg/mod.py": "x = 1\n"})
    repository = make_repository(project)
    repository.inventory()
    assert repository.read("pkg/mod.py").text == "x = 1\n"
    assert repository.is_eligible("pkg/mod.py")


def test_modified_snapshot_is_skipped(project: Path, monkeypatch):
    write_files(project, {"pkg/mod.py": "x = 1\n"})
    repository = make_repository(project)
    repository.inventory()
    real_fstat = os.fstat

    def changed(fd):
        info = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat_module.S_IFREG,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns + 1,
        )

    monkeypatch.setattr(repository_module.os, "fstat", changed)
    assert repository.try_read("pkg/mod.py") is None
    assert any(item.reason == OmissionReason.UNREADABLE for item in repository.omissions())


def strict_repository(root: Path, **overrides) -> Repository:
    return Repository(
        root,
        ContextConfig(**overrides),
        state_dir=".lean-weave",
        strict_source=True,
    )


def test_strict_scan_distinguishes_empty_and_uncertain(project: Path):
    with strict_repository(project).scan_python_sources() as scan:
        assert scan.snapshots == ()
        assert scan.complete is True
        assert scan.revalidate() is True

    write_files(project, {"pkg/mod.py": "x = 1\n"})
    with strict_repository(project, visited_entries_max=1).scan_python_sources() as scan:
        assert scan.complete is False
        assert any(reason == "scan_limit" for reason in scan.omission_counts)


def test_strict_scan_respects_ignores_and_protection(project: Path):
    write_files(
        project,
        {
            ".gitignore": "ignored/*\n!ignored/keep.py\n",
            "keep.py": "x = 1\n",
            "ignored/keep.py": "y = 1\n",
            "ignored/drop.py": "z = 1\n",
            "config.yaml": "secret = 1\n",
        },
    )
    repository = Repository(
        project,
        ContextConfig(),
        strict_source=True,
        protected_paths=("config.yaml",),
    )
    with repository.scan_python_sources() as scan:
        assert scan.complete is True
        assert [item.path for item in scan.snapshots] == ["ignored/keep.py", "keep.py"]


def test_strict_scan_quarantines_credentials_without_paths(project: Path):
    secret = "sk-" + "a" * 32
    write_files(project, {"leak.py": f"VALUE = '{secret}'\n", "ok.py": "x = 1\n"})
    with strict_repository(project).scan_python_sources() as scan:
        assert [item.path for item in scan.snapshots] == ["ok.py"]
        assert scan.omission_counts.get("secret") == 1
        assert secret not in repr(scan)


def test_strict_scan_omits_oversized_but_refuses_bad_ignore(project: Path):
    write_files(project, {"big.py": "x" * 64, "ok.py": "x = 1\n"})
    with strict_repository(project, candidate_bytes_max=32).scan_python_sources() as scan:
        assert scan.complete is True
        assert scan.omission_counts.get("oversized") == 1
        assert "big.py" not in [item.path for item in scan.snapshots]

    (project / ".gitignore").write_text("x" * 64)
    with strict_repository(project, candidate_bytes_max=32).scan_python_sources() as scan:
        assert scan.complete is False


def test_strict_scan_rejects_hardlinks(project: Path):
    write_files(project, {"one.py": "x = 1\n"})
    os.link(project / "one.py", project / "two.py")
    with strict_repository(project).scan_python_sources() as scan:
        assert scan.complete is True
        assert scan.snapshots == ()


def test_strict_scan_revalidation_detects_change(project: Path):
    write_files(project, {"a.py": "x = 1\n"})
    repository = strict_repository(project)
    with repository.scan_python_sources() as scan:
        assert scan.revalidate() is True
        (project / "a.py").write_text("x = 2\n")
        assert scan.revalidate() is False

    (project / ".gitignore").write_text("ignored.py\n")
    with strict_repository(project).scan_python_sources() as scan:
        assert scan.revalidate() is True
        (project / ".gitignore").write_text("other.py\n")
        assert scan.revalidate() is False


def test_validate_index_source_path_is_canonical():
    assert validate_index_source_path("pkg/a.py") == "pkg/a.py"
    for value in ("./pkg/a.py", "/pkg/a.py", "pkg/a.txt", "pkg/../a.py", "C:/a.py"):
        with pytest.raises(InvalidContextRequest):
            validate_index_source_path(value)


def test_scoped_retrieval_scan_filters_before_body_open(project: Path, monkeypatch):
    write_files(
        project,
        {
            "src/a.py": "A = 1\n",
            "src/b.py": "B = 1\n",
            "src/c.py": "C = 1\n",
            "notes.txt": "notes\n",
        },
    )
    repository = Repository(project, ContextConfig(), strict_source=True)
    opened: list[str] = []
    original = repository_module._StrictScanner._read_strict

    def recording(self, dir_fd, name, expected, rel):
        opened.append(rel)
        return original(self, dir_fd, name, expected, rel)

    monkeypatch.setattr(repository_module._StrictScanner, "_read_strict", recording)
    with repository.scan_retrieval_sources(
        paths=frozenset({"src/a.py", "src/c.py"}),
        read_filter=lambda path: path != "src/c.py",
    ) as scan:
        assert scan.complete is True
        assert [item.path for item in scan.snapshots] == ["src/a.py"]
    assert "src/b.py" not in opened
    assert "src/c.py" not in opened


def test_empty_scoped_scan_admits_no_source(project: Path):
    write_files(project, {"src/a.py": "A = 1\n"})
    repository = Repository(project, ContextConfig(), strict_source=True)
    with repository.scan_retrieval_sources(paths=frozenset()) as scan:
        assert scan.complete is True
        assert scan.snapshots == ()


def test_scoped_snapshot_keeps_raw_and_canonical_revisions(project: Path):
    write_files(project, {"src/a.py": "A = 1\r\n"})
    repository = Repository(project, ContextConfig(), strict_source=True)
    with repository.scan_retrieval_sources(paths=frozenset({"src/a.py"})) as scan:
        snapshot = scan.snapshots[0]
        assert snapshot.raw_revision is not None
        assert snapshot.raw_revision.startswith("sha256:")
        assert snapshot.raw_revision != snapshot.digest
