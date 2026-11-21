"""Bounded repository inventory and safe content admission.

Path discovery never reads candidate file bodies. Content is admitted only on
demand through :meth:`Repository.read`, which validates containment and every
path component again, uses bounded no-follow reads, and caches one canonical
snapshot per file for the whole operation.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

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
    is_python,
    is_supported_text,
    safe_root,
)
from lean_weave.errors import ConfigError, ContextScanError, ForbiddenContext, InvalidContextRequest
from lean_weave.logging_setup import get_logger

log = get_logger("context.repository")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STRICT_DEPTH_MAX = 128
_READ_CHUNK = 65536


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


@dataclass(frozen=True)
class PythonSourceScan:
    """Strict Python admission result for one observed checkout generation."""

    snapshots: tuple[FileSnapshot, ...]
    complete: bool
    omission_counts: Mapping[str, int]
    _session: "_StrictScanner | None" = field(default=None, repr=False, compare=False)

    def revalidate(self) -> bool:
        """Re-check observed identities without reading any body again."""

        if self._session is None:
            return False
        return self._session.revalidate()


def validate_index_source_path(
    path: str,
    *,
    protected_paths: tuple[str, ...] = (),
) -> str:
    """Return a canonical Python source path or raise a safe request error.

    The value must already be canonical: normalization may not rewrite it.
    """

    try:
        normalized = normalize_relative_path(path)
    except InvalidContextRequest:
        raise
    if normalized != path:
        raise InvalidContextRequest("Index source path is not canonical")
    try:
        normalized.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise InvalidContextRequest("Index source path is not valid UTF-8") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise InvalidContextRequest("Index source path contains control characters")
    if len(normalized) > 1 and normalized[1] == ":":
        raise InvalidContextRequest("Index source path must not contain a drive prefix")
    if not is_python(normalized):
        raise InvalidContextRequest("Index source path must be Python")
    if contains_secret(normalized):
        raise ForbiddenContext("Index source path was rejected by the credential filter")
    if normalized in protected_paths:
        raise ForbiddenContext("Index source path is protected")
    return normalized


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
        stat.S_IFMT(info.st_mode),
    )


class Repository:
    """One project-root-scoped view of eligible paths and admitted snapshots."""

    def __init__(
        self,
        root: Path | None = None,
        config: ContextConfig | None = None,
        *,
        state_dir: str = ".lean-weave",
        strict_source: bool = False,
        protected_paths: tuple[str, ...] = (),
    ) -> None:
        self.root = safe_root(root)
        self.config = config if config is not None else ContextConfig()
        self.state_dir = state_dir
        self.strict_source = strict_source
        self.protected_paths = tuple(sorted(set(protected_paths)))
        self._validate_state_dir()
        for path in self.protected_paths:
            normalize_relative_path(path)
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
            if contains_secret(rel):
                # Secret-like names are never inventoried or rendered.
                omissions.append(Omission(reason=OmissionReason.SECRET))
                continue
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
        except (re.error, ValueError, TypeError):
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

    @contextlib.contextmanager
    def scan_python_sources(self) -> Iterator[PythonSourceScan]:
        """Admit Python sources through the strict, descriptor-relative path.

        The context keeps the anchored root descriptor open so the caller can
        revalidate observations immediately before publishing.
        """

        if not self.strict_source:
            raise ContextScanError("Strict source admission is not enabled")
        scanner = _StrictScanner(self)
        scanner.open()
        try:
            yield scanner.run()
        finally:
            scanner.close()

    def omissions(self) -> list[Omission]:
        inventory = self.inventory()
        return [*inventory.omissions, *self._omissions.values()]

    def _record(self, path: str | None, reason: OmissionReason, detail: str | None = None) -> None:
        safe_path = path if path is not None and not contains_secret(path) else None
        key = safe_path if safe_path is not None else f"<{reason.value}:{len(self._omissions)}>"
        self._omissions.setdefault(
            key, Omission(reason=reason, path=safe_path, detail=detail)
        )

    def _admit(self, normalized: str) -> FileSnapshot | None:
        if contains_secret(normalized):
            self._record(None, OmissionReason.SECRET)
            return None
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


class _StrictScanner:
    """One anchored, descriptor-relative scan with a bounded witness set."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.config = repository.config
        self.root_fd = -1
        self._visited = 0
        self._bytes_read = 0
        self._complete = True
        self._counts: dict[str, int] = {}
        self._root_witness: tuple[int, ...] | None = None
        self._dir_witnesses: list[tuple[str, tuple[int, ...]]] = []
        self._file_witnesses: list[tuple[str, str, tuple[int, ...]]] = []
        self._absent_ignore: list[tuple[str, str]] = []
        self._snapshots: list[FileSnapshot] = []
        self._ignore_specs: list[tuple[str, pathspec.PathSpec]] = []
        self._ignore_bases: list[int] = []

    # ------------------------------------------------------------------
    # Lifecycle and primitive support
    # ------------------------------------------------------------------
    def open(self) -> None:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise ContextScanError("Strict source admission is unavailable on this platform")
        if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
            raise ContextScanError("Strict source admission is unavailable on this platform")
        if os.scandir not in os.supports_fd:
            raise ContextScanError("Strict source admission is unavailable on this platform")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self.root_fd = os.open(self.repository.root, flags)
        except OSError:
            raise ContextScanError("Cannot open the project root for strict admission") from None
        try:
            info = os.fstat(self.root_fd)
        except OSError:
            self.close()
            raise ContextScanError("Cannot inspect the project root") from None
        if not stat.S_ISDIR(info.st_mode):
            self.close()
            raise ContextScanError("Project root is not a directory")
        self._root_witness = _identity(info)
        self._dir_witnesses.append(("", _identity(info)))

    def close(self) -> None:
        if self.root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.root_fd)
            self.root_fd = -1

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------
    def _count(self, reason: str) -> None:
        self._counts[reason] = self._counts.get(reason, 0) + 1

    def _incomplete(self, reason: str) -> None:
        self._count(reason)
        self._complete = False

    def _remaining_bytes(self) -> int:
        return self.config.scan_bytes_max - self._bytes_read

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> PythonSourceScan:
        try:
            self._walk_dir(self.root_fd, "", 0)
            complete = self._complete
            if not complete:
                log.warning("[Context.strict] scan incomplete; publication is refused")
            log.debug(
                "[Context.strict] admitted=%s omissions=%s visited=%s complete=%s",
                len(self._snapshots),
                sum(self._counts.values()),
                self._visited,
                complete,
            )
            return PythonSourceScan(
                snapshots=tuple(sorted(self._snapshots, key=lambda item: item.path)),
                complete=complete,
                omission_counts=dict(self._counts),
                _session=self,
            )
        except BaseException:
            self._complete = False
            raise

    # ------------------------------------------------------------------
    # Ignore rules
    # ------------------------------------------------------------------
    def _walk_dir(self, dir_fd: int, rel_dir: str, depth: int) -> None:
        if depth > _STRICT_DEPTH_MAX:
            self._incomplete("scan_limit")
            return
        if not self._load_ignore_file(dir_fd, rel_dir):
            return
        remaining = self.config.visited_entries_max - self._visited
        try:
            with os.scandir(dir_fd) as iterator:
                entries: list[os.DirEntry] = []
                for entry in iterator:
                    entries.append(entry)
                    if len(entries) > max(remaining, 0):
                        break
        except OSError:
            self._incomplete("unreadable")
            return
        if remaining < 0 or len(entries) > remaining:
            # Never publish a partial batch from a directory.
            self._incomplete("scan_limit")
            return
        self._visited += len(entries)
        try:
            ordered = sorted(entries, key=lambda item: item.name)
        except (OSError, ValueError):
            self._incomplete("unreadable")
            return
        for entry in ordered:
            try:
                self._visit(entry, dir_fd, rel_dir, depth)
            except OSError:
                # Any descriptor/stat failure while visiting is uncertainty.
                self._incomplete("unreadable")
                continue
        self._pop_ignore(rel_dir)

    def _load_ignore_file(self, dir_fd: int, rel_dir: str) -> bool:
        name = ".gitignore"
        rel = f"{rel_dir}/.gitignore" if rel_dir else ".gitignore"
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            self._absent_ignore.append((rel_dir, name))
            return True
        except OSError:
            self._incomplete("ignore_unreadable")
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            self._incomplete("ignore_indeterminate")
            return False
        self._file_witnesses.append((rel_dir, name, _identity(info)))
        if info.st_nlink != 1:
            self._incomplete("ignore_indeterminate")
            return False
        if info.st_size > self.config.candidate_bytes_max:
            self._incomplete("ignore_oversized")
            return False
        status, snapshot = self._read_strict(dir_fd, name, info, rel)
        if status != "ok" or snapshot is None:
            self._incomplete("ignore_indeterminate")
            return False
        lines = snapshot.text.split("\n")
        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
        except (re.error, ValueError, TypeError):
            self._incomplete("ignore_indeterminate")
            return False
        self._ignore_specs.append((rel_dir, spec))
        self._ignore_bases.append(len(self._ignore_specs) - 1)
        return True

    def _pop_ignore(self, rel_dir: str) -> None:
        while self._ignore_specs and self._ignore_specs[-1][0] == rel_dir:
            self._ignore_specs.pop()

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
        config_spec = self.repository._config_spec
        if config_spec is not None:
            try:
                result = config_spec.check_file(target)
            except (ValueError, TypeError):
                result = None
            if result is not None and result.include is True:
                ignored = True
        return ignored

    # ------------------------------------------------------------------
    # Visits
    # ------------------------------------------------------------------
    def _visit(self, entry: os.DirEntry, dir_fd: int, rel_dir: str, depth: int) -> None:
        name = entry.name
        rel = f"{rel_dir}/{name}" if rel_dir else name
        if contains_secret(rel):
            self._count("secret")
            return
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            self._count("unreadable")
            return
        if stat.S_ISLNK(info.st_mode):
            self._count("excluded")
            return
        if stat.S_ISDIR(info.st_mode):
            self._visit_dir(entry, info, dir_fd, rel, depth)
            return
        if not stat.S_ISREG(info.st_mode):
            self._count("unsupported")
            return
        if is_hard_denied(rel, state_dir=self.repository.state_dir):
            self._count("hard_denied")
            return
        if self._is_ignored(rel, is_dir=False):
            self._count("excluded")
            return
        if not is_python(rel):
            self._count("unsupported")
            return
        if rel in self.repository.protected_paths:
            self._count("excluded")
            return
        self._file_witnesses.append((rel_dir, name, _identity(info)))
        if info.st_nlink != 1:
            self._count("excluded")
            return
        if info.st_size > self.config.candidate_bytes_max:
            self._count("oversized")
            return
        status, snapshot = self._read_strict(dir_fd, name, info, rel)
        if status == "ok" and snapshot is not None:
            self._snapshots.append(snapshot)
        elif status in {"binary", "undecodable", "secret"}:
            self._count(status)
        # Other non-ok statuses already marked the scan incomplete.

    def _visit_dir(
        self,
        entry: os.DirEntry,
        info: os.stat_result,
        parent_fd: int,
        rel: str,
        depth: int,
    ) -> None:
        if is_hard_denied_dir(rel, state_dir=self.repository.state_dir):
            self._count("hard_denied")
            return
        if self._is_ignored(rel, is_dir=True):
            self._count("excluded")
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            child_fd = os.open(entry.name, flags, dir_fd=parent_fd)
        except OSError:
            self._incomplete("unreadable")
            return
        try:
            opened = os.fstat(child_fd)
            if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(info):
                self._incomplete("unreadable")
                return
            self._dir_witnesses.append((rel, _identity(opened)))
            # Probe for a nested repository without following links.
            try:
                os.stat(".git", dir_fd=child_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                self._incomplete("unreadable")
                return
            else:
                self._count("hard_denied")
                return
            self._walk_dir(child_fd, rel, depth + 1)
        finally:
            with contextlib.suppress(OSError):
                os.close(child_fd)

    # ------------------------------------------------------------------
    # Strict body reads
    # ------------------------------------------------------------------
    def _read_strict(
        self, dir_fd: int, name: str, expected: os.stat_result, rel: str
    ) -> tuple[str, FileSnapshot | None]:
        allowed = self._remaining_bytes()
        if expected.st_size > 0 and expected.st_size > allowed:
            self._incomplete("scan_limit")
            return "scan_limit", None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError:
            self._incomplete("unreadable")
            return "unreadable", None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(expected):
                self._incomplete("unreadable")
                return "unreadable", None
            cap = min(self.config.candidate_bytes_max, max(allowed, 0))
            chunks: list[bytes] = []
            total = 0
            while total <= cap:
                chunk = os.read(fd, min(_READ_CHUNK, cap + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            after = os.fstat(fd)
        except OSError:
            self._incomplete("unreadable")
            return "unreadable", None
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._bytes_read += total
        if total > cap:
            # Growth or insufficient budget during the read is uncertainty.
            self._incomplete("scan_limit" if allowed < self.config.candidate_bytes_max else "unreadable")
            return "incomplete", None
        if _identity(after) != _identity(opened):
            self._incomplete("unreadable")
            return "incomplete", None
        try:
            path_info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            self._incomplete("unreadable")
            return "incomplete", None
        if _identity(path_info) != _identity(opened):
            self._incomplete("unreadable")
            return "incomplete", None
        data = b"".join(chunks)
        if b"\x00" in data:
            return "binary", None
        text = decode_snapshot(data)
        if text is None:
            return "undecodable", None
        if contains_secret(text):
            log.warning("[Context.strict] quarantined a candidate by the credential filter")
            return "secret", None
        return "ok", FileSnapshot(rel, text, content_digest(text), len(data))

    # ------------------------------------------------------------------
    # Witness revalidation
    # ------------------------------------------------------------------
    def revalidate(self) -> bool:
        if self.root_fd < 0 or self._root_witness is None:
            return False
        try:
            if _identity(os.fstat(self.root_fd)) != self._root_witness:
                return False
        except OSError:
            return False
        for rel_dir, witness in self._dir_witnesses:
            if not rel_dir:
                continue
            info = self._stat_directory(rel_dir)
            if info is None or _identity(info) != witness:
                return False
        for rel_dir, name, witness in self._file_witnesses:
            parent = self._open_directory(rel_dir)
            if parent is None:
                return False
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                return False
            finally:
                with contextlib.suppress(OSError):
                    os.close(parent)
            if _identity(info) != witness:
                return False
        for rel_dir, name in self._absent_ignore:
            parent = self._open_directory(rel_dir)
            if parent is None:
                return False
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            else:
                return False
            finally:
                with contextlib.suppress(OSError):
                    os.close(parent)
        return True

    def _open_directory(self, rel_dir: str) -> int | None:
        if self.root_fd < 0:
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            current = os.open(f".", flags, dir_fd=self.root_fd)
        except OSError:
            return None
        if not rel_dir:
            return current
        try:
            for part in rel_dir.split("/"):
                try:
                    child = os.open(part, flags, dir_fd=current)
                except OSError:
                    return None
                with contextlib.suppress(OSError):
                    os.close(current)
                current = child
            return current
        except OSError:
            with contextlib.suppress(OSError):
                os.close(current)
            return None

    def _stat_directory(self, rel_dir: str) -> os.stat_result | None:
        fd = self._open_directory(rel_dir)
        if fd is None:
            return None
        try:
            return os.fstat(fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
