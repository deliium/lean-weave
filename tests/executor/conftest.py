import os
from pathlib import Path

from lean_weave.config.models import AppConfig, ToolsConfig
from lean_weave.executor.approval import approve_tools


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def make_approval(root: Path, *, writes: bool = False, checks: bool = False, **tools):
    config = AppConfig(tools=ToolsConfig(**tools)) if tools else AppConfig()
    return approve_tools(root, config, allow_writes=writes, allow_checks=checks)


def snapshot_bytes(root: Path) -> dict[str, bytes]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found
