"""Bounded repository inventory and safe content admission.

Path discovery never reads candidate file bodies. Content is admitted only on
demand through :meth:`Repository.read`, which validates containment and every
path component again, uses bounded no-follow reads, and caches one canonical
snapshot per file for the whole operation.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from lean_weave.config.models import ContextConfig
from lean_weave.context.models import (
    Omission,
    OmissionReason,
    content_digest,
    normalize_relative_path,
)
from lean_weave.context.policy import (
    contains_secret,
    decode_snapshot,
    is_hard_denied,
    is_hard_denied_dir,
    is_supported_text,
    safe_root,
)
from lean_weave.errors import ConfigError, ForbiddenContext
from lean_weave.logging_setup import get_logger

log = get_logger("context.repository")


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    text: str
    digest: str
    size_bytes: int

    @property
    def lines(self) -> int:
        if not self.text:
            return 0
        return self.text.count("\n") + (0 if self.text.endswith("\n") else 1)


@dataclass
class Inventory:
    files: list[str] = field(default_factory=list)
    omissions: list[Omission] = field(default_factory=list)
    visited: int = 0
    complete: bool = True


class Repository:
    """One project-root-scoped view of eligible paths and admitted snapshots."""

    def __init__(
        self,
        root: Path | None = None,
        config: ContextConfig | None = None,
        *,
        state_dir: str = ".lean-weave",
    ) -> None:
        self.root = safe_root(root)
        self.config = config if config is not None else ContextConfig()
        self.state_dir = state_dir
        self._validate_state_dir()
        self._inventory: Inventory | None = None
        self._eligible_set: set[str] = set()
        self._snapshots: dict[str, FileSnapshot] = {}
        self._omissions: dict[str, Omission] = {}
        self._secret_paths: set[str] = set()
        self._visited = 0
        self._bytes_read = 0
        self._ignore_specs: list[tuple[str, pathspec.PathSpec]] = []
        self._config_spec: pathspec.PathSpec | None = None
        exclusions = self.config.exclusions
        if exclusions:
            try:
                self._config_spec = pathspec.PathSpec.from_lines("gitwildmatch", exclusions)
            except (re.error, ValueError, TypeError):  # type: ignore[name-defined]
                raise ConfigError("Invalid context.exclusions pattern") from None
        log.debug(
            "[Context.repository] root=%s exclusions=%s candidate_bytes_max=%s scan_bytes_max=%s visited_entries_max=%s",
            self.root,
            len(exclusions),
            self.config.candidate_bytes_max,
            self.config.scan_bytes_max,
            self.config.visited_entries_max,
        )

    # ------------------------------------------------------------------
    # Configuration / containment
    # ------------------------------------------------------------------
    def _validate_state_dir(self) -> None:
        try:
            state = (self.root / self.state_dir).resolve()
        except (OSError, RuntimeError, ValueError):
            raise ConfigError("Cannot resolve configuration field: workflow.state_dir") from None
        if not state.is_relative_to(self.root):
            raise ConfigError("Invalid configuration field: workflow.state_dir (outside project root)")
        if state == self.root:
            raise ConfigError("workflow.state_dir must not resolve to the project root")

    def relative(self, path: str) -> str:
        return normalize_relative_path(path)

    def absolute(self, path: str) -> Path:
        return self.root / normalize_relative_path(path)

    # ------------------------------------------------------------------
    # Inventory (path-only)
    # ------------------------------------------------------------------
    def inventory(self) -> Inventory:
        if self._inventory is not None:
            return self._inventory
        files: list[str] = []
        omissions: list[Omission] = []
        self._visited = 0
        self._ignore_specs = []
        complete = self._walk_dir("", files, omissions)
        self._inventory = Inventory(
            files=sorted(files), omissions=omissions, visited=self._visited, complete=complete
        )
        self._eligible_set = set(self._inventory.files)
        log.debug(
            "[Context.inventory] files=%s omissions=%s visited=%s complete=%s",
            len(files),
            len(omissions),
            self._visited,
            complete,
        )
        if not complete:
            log.warning("[Context.inventory] scan incomplete; results are bounded")
        return self._inventory

    def _walk_dir(self, rel_dir: str, files: list[str], omissions: list[Omission]) -> bool:
        self._load_ignore_file(rel_dir)
        directory = self.root / rel_dir if rel_dir else self.root
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            omissions.append(Omission(reason=OmissionReason.UNREADABLE, detail="directory"))
            return True

        for entry in entries:
            if self._visited >= self.config.visited_entries_max:
                omissions.append(Omission(reason=OmissionReason.SCAN_LIMIT, detail="visited_entries"))
                return False
            self._visited += 1
            name = entry.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                omissions.append(Omission(reason=OmissionReason.UNREADABLE, path=rel, detail="stat"))
                continue
            if stat.S_ISLNK(info.st_mode):
                # Never follow file or directory symlinks, including dangling ones.
                omissions.append(Omission(reason=OmissionReason.EXCLUDED, path=rel, detail="symlink"))
                continue
            if stat.S_ISDIR(info.st_mode):
                if is_hard_denied_dir(rel, state_dir=self.state_dir):
                    omissions.append(
                        Omission(reason=OmissionReason.HARD_DENIED, path=rel, detail="directory")
                    )
                    continue
                if (self.root / rel / ".git").exists():
                    # Never traverse nested repositories.
                    omissions.append(
                        Omission(reason=OmissionReason.HARD_DENIED, path=rel, detail="nested_repository")
                    )
                    continue
                if self._is_ignored(rel, is_dir=True):
                    omissions.append(
                        Omission(reason=OmissionReason.EXCLUDED, path=rel, detail="gitignore")
                    )
                    continue
                if not self._walk_dir(rel, files, omissions):
                    return False
                continue
            if not stat.S_ISREG(info.st_mode):
                omissions.append(
                    Omission(reason=OmissionReason.UNSUPPORTED, path=rel, detail="not_regular")
                )
                continue
            if is_hard_denied(rel, state_dir=self.state_dir):
                omissions.append(
                    Omission(reason=OmissionReason.HARD_DENIED, path=rel, detail="name")
                )
                continue
            if self._is_ignored(rel, is_dir=False):
                omissions.append(Omission(reason=OmissionReason.EXCLUDED, path=rel, detail="gitignore"))
                continue
            if not is_supported_text(rel):
                omissions.append(
                    Omission(reason=OmissionReason.UNSUPPORTED, path=rel, detail="text_type")
                )
                continue
            files.append(rel)
        return True

    def _load_ignore_file(self, rel_dir: str) -> None:
        rel = f"{rel_dir}/.gitignore" if rel_dir else ".gitignore"
        candidate = self.root / rel
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        if info.st_size > self.config.candidate_bytes_max:
            return
        if self._bytes_read + info.st_size > self.config.scan_bytes_max:
            return
        try:
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return
        try:
            data = os.read(fd, self.config.candidate_bytes_max + 1)
        finally:
            os.close(fd)
        self._bytes_read += len(data)
        if len(data) > self.config.candidate_bytes_max or b"\x00" in data:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
        except (ValueError, TypeError):
            return
        self._ignore_specs.append((rel_dir, spec))

    def _is_ignored(self, rel: str, *, is_dir: bool) -> bool:
        target = f"{rel}/" if is_dir else rel
        ignored = False
        for base, spec in self._ignore_specs:
            if base:
                prefix = f"{base}/"
                if not rel.startswith(prefix):
                    continue
                sub = rel[len(prefix):]
            else:
                sub = rel
            if is_dir:
                sub = f"{sub}/"
            try:
                result = spec.check_file(sub)
            except (ValueError, TypeError):
                continue
            if result.include is not None:
                ignored = bool(result.include)
        if self._config_spec is not None:
            try:
                result = self._config_spec.check_file(target)
            except (ValueError, TypeError):
                result = None
            # Configured exclusions are additive: negation cannot un-exclude.
            if result is not None and result.include is True:
                ignored = True
        return ignored

    # ------------------------------------------------------------------
    # Safe content admission
    # ------------------------------------------------------------------
    def is_eligible(self, path: str) -> bool:
        try:
            normalized = normalize_relative_path(path)
        except Exception:
            return False
        self.inventory()
        return normalized in self._eligible_set

    def read(self, path: str) -> FileSnapshot:
        """Read an eligible path or raise a safe, generic context error."""

        normalized = normalize_relative_path(path)
        if normalized in self._snapshots:
            return self._snapshots[normalized]
        if normalized in self._secret_paths:
            raise ForbiddenContext("Requested path is not available for context retrieval")
        if not self.is_eligible(normalized):
            raise ForbiddenContext("Requested path is not available for context retrieval")
        snapshot = self._admit(normalized)
        if snapshot is None:
            raise ForbiddenContext("Requested path is not available for context retrieval")
        return snapshot

    def try_read(self, path: str) -> FileSnapshot | None:
        try:
            normalized = normalize_relative_path(path)
        except Exception:
            return None
        if normalized in self._snapshots:
            return self._snapshots[normalized]
        if not self.is_eligible(normalized):
            self._record(normalized, OmissionReason.NOT_ELIGIBLE)
            return None
        return self._admit(normalized)

    def omissions(self) -> list[Omission]:
        inventory = self.inventory()
        return [*inventory.omissions, *self._omissions.values()]

    def _record(self, path: str | None, reason: OmissionReason, detail: str | None = None) -> None:
        key = path if path is not None else f"<{reason.value}:{len(self._omissions)}>"
        self._omissions.setdefault(key, Omission(reason=reason, path=path, detail=detail))

    def _admit(self, normalized: str) -> FileSnapshot | None:
        parts = normalized.split("/")
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = os.lstat(current)
            except OSError:
                self._record(normalized, OmissionReason.UNREADABLE, "missing")
                return None
            if stat.S_ISLNK(info.st_mode):
                self._record(normalized, OmissionReason.EXCLUDED, "symlink")
                return None
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                self._record(normalized, OmissionReason.UNREADABLE, "component")
                return None
        try:
            info = os.lstat(current)
        except OSError:
            self._record(normalized, OmissionReason.UNREADABLE, "stat")
            return None
        if not stat.S_ISREG(info.st_mode):
            self._record(normalized, OmissionReason.UNSUPPORTED, "not_regular")
            return None
        if info.st_size > self.config.candidate_bytes_max:
            self._record(normalized, OmissionReason.OVERSIZED, "candidate")
            return None
        if self._bytes_read + info.st_size > self.config.scan_bytes_max:
            self._record(normalized, OmissionReason.SCAN_LIMIT, "scan_bytes")
            return None

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(current, flags)
        except OSError:
            self._record(normalized, OmissionReason.UNREADABLE, "open")
            return None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                self._record(normalized, OmissionReason.UNSUPPORTED, "not_regular")
                return None
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            ):
                self._record(normalized, OmissionReason.UNREADABLE, "changed")
                return None
            data = os.read(fd, self.config.candidate_bytes_max + 1)
        except OSError:
            self._record(normalized, OmissionReason.UNREADABLE, "read")
            return None
        finally:
            os.close(fd)
        self._bytes_read += len(data)
        if len(data) > self.config.candidate_bytes_max:
            self._record(normalized, OmissionReason.OVERSIZED, "candidate")
            return None
        if b"\x00" in data:
            self._record(normalized, OmissionReason.BINARY)
            return None
        text = decode_snapshot(data)
        if text is None:
            self._record(normalized, OmissionReason.UNDECODABLE)
            return None
        if contains_secret(text):
            # Quarantine before path, signature, or source exposure.
            self._secret_paths.add(normalized)
            self._record(None, OmissionReason.SECRET, "credential_pattern")
            log.warning("[Context.admit] quarantined a candidate by the credential filter")
            return None
        snapshot = FileSnapshot(normalized, text, content_digest(text), len(data))
        self._snapshots[normalized] = snapshot
        log.debug("[Context.admit] chars=%s", len(text))
        return snapshot

    def refresh(self) -> None:
        """Drop cached inventory/snapshots; the next operation scans again."""

        self._inventory = None
        self._eligible_set.clear()
        self._snapshots.clear()
        self._omissions.clear()
        self._secret_paths.clear()
        self._bytes_read = 0
