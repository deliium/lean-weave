"""Workspace authorization and one-shot content snapshots.

Inventory membership and lexical normalization are not authorization. Every
read re-checks policy and opens the file through root-anchored, no-follow
directory handles. The credential filter is a bounded high-confidence check,
not a universal secret detector.

An applicable ``.gitignore`` that is symlinked, unreadable, malformed, or over
budget makes its subtree indeterminate: explicit operations are denied and
discovery is incomplete, without disclosing the omitted paths. A missing
``.gitignore`` is normal.

Writes require ``O_NOFOLLOW`` and a single hard link. This layer does not
defend against untrusted concurrent directory mutation; use a controlled
workspace.
"""

from __future__ import annotations

import codecs
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex

import pathspec

from lean_weave.context.policy import (
    contains_secret,
    is_hard_denied,
    is_hard_denied_dir,
    is_supported_text,
)
from lean_weave.executor.approval import ToolApproval
from lean_weave.executor.models import file_revision, normalize_tool_path
from lean_weave.logging_setup import get_logger

log = get_logger("executor.workspace")

_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def _path_lock(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock

_COOKIE = re.compile(rb"^[ \t#]*coding[:=][ \t]*([-_.A-Za-z0-9]+)")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class PhysicalLine:
    content: str
    terminator: str


@dataclass(frozen=True)
class TextSnapshot:
    path: str
    data: bytes
    revision: str
    encoding: str
    bom: bool
    lines: tuple[PhysicalLine, ...]
    newline: str
    identity: tuple[int, int, int, int, int]
    parent_identity: tuple[int, int]
    mode: int

    @property
    def ends_with_newline(self) -> bool:
        return bool(self.lines) and self.lines[-1].terminator != ""


@dataclass(frozen=True)
class Discovery:
    files: tuple[str, ...]
    complete: bool
    omissions: tuple[str, ...]


@dataclass
class _Budget:
    visited: int = 0
    bytes_read: int = 0
    incomplete: bool = False
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def split_physical(text: str) -> tuple[tuple[PhysicalLine, ...], str]:
    """Split text into logical lines while keeping each physical terminator."""

    lines: list[PhysicalLine] = []
    kinds: set[str] = set()
    index = 0
    length = len(text)
    while index < length:
        carriage = text.find("\r", index)
        linefeed = text.find("\n", index)
        if carriage < 0 and linefeed < 0:
            lines.append(PhysicalLine(text[index:], ""))
            break
        if carriage >= 0 and (linefeed < 0 or carriage < linefeed):
            if carriage + 1 < length and text[carriage + 1] == "\n":
                lines.append(PhysicalLine(text[index:carriage], "\r\n"))
                kinds.add("crlf")
                index = carriage + 2
            else:
                lines.append(PhysicalLine(text[index:carriage], "\r"))
                kinds.add("cr")
                index = carriage + 1
            continue
        lines.append(PhysicalLine(text[index:linefeed], "\n"))
        kinds.add("lf")
        index = linefeed + 1
    if not kinds:
        return tuple(lines), "none"
    if len(kinds) == 1:
        return tuple(lines), next(iter(kinds))
    return tuple(lines), "mixed"


def encode_snapshot(encoding: str, bom: bool, lines: tuple[PhysicalLine, ...] | list[PhysicalLine]) -> bytes:
    text = "".join(line.content + line.terminator for line in lines)
    body = text.encode(encoding)
    if bom:
        return b"\xef\xbb\xbf" + body
    return body


def _cookie_encoding(payload: bytes) -> str | None:
    head = payload.splitlines()[:2]
    for raw in head:
        match = _COOKIE.match(raw)
        if match is None:
            continue
        name = match.group(1).decode("ascii", "replace")
        try:
            codecs.lookup(name)
        except LookupError:
            return ""
        return name
    return None


def decode_file(path: str, data: bytes) -> tuple[str, bool, tuple[PhysicalLine, ...], str] | str:
    """Decode one admitted file. Return a decision code on failure."""

    if b"\x00" in data:
        return "binary"
    bom = data.startswith(b"\xef\xbb\xbf")
    payload = data[3:] if bom else data
    encoding = "utf-8"
    if path.endswith((".py", ".pyi")) and not bom:
        cookie = _cookie_encoding(payload)
        if cookie == "":
            return "undecodable"
        if cookie:
            encoding = cookie
    try:
        text = payload.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return "undecodable"
    lines, newline = split_physical(text)
    return encoding, bom, lines, newline


class Workspace:
    """One explicit workspace root and its approval snapshot."""

    # Test seam: runs after the snapshot is taken and before the commit recheck.
    commit_probe = None

    def __init__(self, approval: ToolApproval) -> None:
        self.approval = approval
        self.root = approval.root
        self._budget = _Budget()
        self._specs: list[tuple[str, pathspec.PathSpec]] = []
        self._bad_dirs: set[str] = set()
        self._ignore_loaded: set[str] = set()
        self._exclude_spec = self._compile(approval.excludes)
        self._read_spec = self._compile(approval.read_allow)
        self._write_spec = self._compile(approval.write_allow)

    def _compile(self, patterns: tuple[str, ...]) -> pathspec.PathSpec | None:
        if not patterns:
            return None
        try:
            return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        except (re.error, ValueError, TypeError):
            log.debug("[Tools.workspace] outcome=invalid_pattern")
            return pathspec.PathSpec.from_lines("gitwildmatch", ["*"])

    def begin(self) -> None:
        self._budget = _Budget()
        self._specs = []
        self._bad_dirs = set()
        self._ignore_loaded = set()

    def _note(self, code: str) -> str:
        self._budget.counts[code] = self._budget.counts.get(code, 0) + 1
        return code

    def discover(self) -> Discovery:
        self.begin()
        found: list[str] = []
        self._walk("", found)
        found.sort()
        reasons = tuple(sorted(set(self._budget.reasons)))
        log.debug(
            "[Tools.workspace] operation=discover admitted=%s visited=%s complete=%s reasons=%s",
            len(found),
            self._budget.visited,
            not self._budget.incomplete,
            ",".join(reasons),
        )
        if self._budget.incomplete:
            log.warning("[Tools.workspace] outcome=incomplete")
        return Discovery(files=tuple(found), complete=not self._budget.incomplete, omissions=reasons)

    def _limited(self) -> bool:
        if self._budget.visited >= self.approval.limits.visited_entries_max:
            self._budget.incomplete = True
            self._budget.reasons.append("scan_limit")
            return True
        return False

    def _walk(self, rel_dir: str, found: list[str]) -> None:
        if self._limited():
            return
        if self._load_ignore(rel_dir) == "indeterminate":
            self._budget.incomplete = True
            self._budget.reasons.append("indeterminate")
            self._note("indeterminate")
            return
        directory = self.root / rel_dir if rel_dir else self.root
        try:
            iterator = os.scandir(directory)
        except OSError:
            self._budget.incomplete = True
            self._budget.reasons.append("unreadable")
            return
        with iterator:
            for entry in iterator:
                if self._limited():
                    return
                self._budget.visited += 1
                name = entry.name
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if contains_secret(rel):
                    self._note("secret")
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    self._budget.incomplete = True
                    self._budget.reasons.append("unreadable")
                    self._note("unreadable")
                    continue
                if stat.S_ISLNK(info.st_mode):
                    self._note("symlink")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if is_hard_denied_dir(rel, state_dir=self.approval.state_dir) or self._protected(rel):
                        self._note("hard_denied")
                        continue
                    if self._nested_repo(rel):
                        self._note("hard_denied")
                        continue
                    if self._ignored(rel, is_dir=True):
                        self._note("excluded")
                        continue
                    self._walk(rel, found)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    self._note("unsupported")
                    continue
                decision = self._name_policy(rel, write=False)
                if decision == "ok":
                    found.append(rel)
                    self._note("admitted")
                else:
                    self._note(decision)

    def _nested_repo(self, rel: str) -> bool:
        try:
            os.lstat(self.root / rel / ".git")
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _protected(self, rel: str) -> bool:
        for item in self.approval.protected:
            if rel == item or rel.startswith(item.rstrip("/") + "/"):
                return True
        return False

    def _matches(self, spec: pathspec.PathSpec | None, rel: str, *, is_dir: bool) -> bool:
        if spec is None:
            return False
        target = f"{rel}/" if is_dir else rel
        try:
            result = spec.check_file(target)
        except (ValueError, TypeError):
            return True
        return bool(result.include)

    def _ignored(self, rel: str, *, is_dir: bool) -> bool:
        ignored = False
        for base, spec in self._specs:
            if base:
                prefix = f"{base}/"
                if not (rel == base or rel.startswith(prefix)):
                    continue
                sub = rel[len(prefix):] if rel.startswith(prefix) else ""
            else:
                sub = rel
            if is_dir and sub:
                sub = f"{sub}/"
            try:
                result = spec.check_file(sub)
            except (ValueError, TypeError):
                ignored = True
                continue
            if result.include is not None:
                ignored = bool(result.include)
        if self._matches(self._exclude_spec, rel, is_dir=is_dir):
            ignored = True
        return ignored

    def _charge(self, count: int) -> bool:
        if count < 0:
            count = 0
        if self._budget.bytes_read + count > self.approval.limits.scan_bytes_max:
            self._budget.bytes_read = self.approval.limits.scan_bytes_max + 1
            self._budget.incomplete = True
            self._budget.reasons.append("scan_limit")
            return False
        self._budget.bytes_read += count
        return True

    def _load_ignore(self, rel_dir: str) -> str:
        if rel_dir in self._bad_dirs:
            return "indeterminate"
        if rel_dir in self._ignore_loaded:
            return "ok"
        self._ignore_loaded.add(rel_dir)
        rel = f"{rel_dir}/.gitignore" if rel_dir else ".gitignore"
        candidate = self.root / rel
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            return "ok"
        except OSError:
            self._bad_dirs.add(rel_dir)
            return "indeterminate"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        size = int(info.st_size)
        if size > self.approval.limits.file_bytes_max or not self._charge(min(size, self.approval.limits.file_bytes_max + 1) if size > self.approval.limits.file_bytes_max else size):
            if size > self.approval.limits.file_bytes_max:
                self._charge(1)
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        try:
            fd = os.open(candidate, os.O_RDONLY | _NOFOLLOW)
        except OSError:
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        try:
            data = os.read(fd, self.approval.limits.file_bytes_max + 1)
        except OSError:
            self._bad_dirs.add(rel_dir)
            return "indeterminate"
        finally:
            os.close(fd)
        if not self._charge(0):
            self._bad_dirs.add(rel_dir)
            return "indeterminate"
        if len(data) > self.approval.limits.file_bytes_max or b"\x00" in data:
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", text.splitlines())
        except (re.error, ValueError, TypeError):
            self._bad_dirs.add(rel_dir)
            self._note("indeterminate")
            return "indeterminate"
        self._specs.append((rel_dir, spec))
        return "ok"

    def _ancestors(self, rel: str) -> list[str]:
        parts = rel.split("/")
        dirs = [""]
        if len(parts) > 1:
            current = ""
            for part in parts[:-1]:
                current = f"{current}/{part}" if current else part
                dirs.append(current)
        return dirs

    def _ensure_ignores(self, rel: str) -> str:
        for directory in self._ancestors(rel):
            if self._load_ignore(directory) == "indeterminate":
                return "indeterminate"
            if directory in self._bad_dirs:
                return "indeterminate"
        return "ok"

    def _name_policy(self, rel: str, *, write: bool) -> str:
        if self._protected(rel) or is_hard_denied(rel, state_dir=self.approval.state_dir):
            return "hard_denied"
        if contains_secret(rel):
            return "secret"
        if not is_supported_text(rel):
            return "unsupported"
        if self._ignored(rel, is_dir=False):
            return "excluded"
        if self._read_spec is not None and not self._matches(self._read_spec, rel, is_dir=False):
            return "not_in_scope"
        if write:
            if not self.approval.writes_approved or self._write_spec is None:
                return "denied"
            if not self._matches(self._write_spec, rel, is_dir=False):
                return "denied"
        return "ok"

    def authorize(self, path: str, *, write: bool = False) -> str:
        """Return a stable decision code. The code never contains the path."""

        try:
            rel = normalize_tool_path(path)
        except ValueError:
            return self._note("invalid")
        # Reject a symlink or non-directory component before loading ignore
        # files. A pathname read of ``<symlink>/.gitignore`` would traverse the
        # link before the path itself is denied.
        kind = self._component_kind(rel)
        if kind is not None:
            log.debug("[FIX] authorization rejected code=%s before ignore read", kind)
            return self._note(kind)
        if self._ensure_ignores(rel) == "indeterminate":
            return self._note("indeterminate")
        decision = self._name_policy(rel, write=write)
        if decision != "ok":
            return self._note(decision)
        return self._note("ok")

    def _component_kind(self, rel: str) -> str | None:
        """Classify symlink and non-directory components without following links."""

        current = self.root
        parts = rel.split("/")
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return None if index == len(parts) - 1 else "missing"
            except OSError:
                return "unreadable"
            if stat.S_ISLNK(info.st_mode):
                return "symlink"
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return "unsupported"
        return None

    def read(self, path: str) -> TextSnapshot | str:
        """Re-admit ``path``. Return a snapshot or a decision code."""

        decision = self.authorize(path, write=False)
        if decision != "ok":
            return decision
        rel = normalize_tool_path(path)
        opened = self._read_bytes(rel)
        if isinstance(opened, str):
            return self._note(opened)
        data, identity, parent_identity, mode = opened
        if len(data) > self.approval.limits.file_bytes_max:
            return self._note("oversized")
        decoded = decode_file(rel, data)
        if isinstance(decoded, str):
            return self._note(decoded)
        encoding, bom, lines, newline = decoded
        text = "".join(line.content + line.terminator for line in lines)
        if contains_secret(text):
            log.warning("[Tools.workspace] outcome=secret")
            return self._note("secret")
        self._note("admitted")
        return TextSnapshot(
            path=rel,
            data=data,
            revision=file_revision(data),
            encoding=encoding,
            bom=bom,
            lines=lines,
            newline=newline,
            identity=identity,
            parent_identity=parent_identity,
            mode=mode,
        )

    def _read_bytes(self, rel: str) -> tuple[bytes, tuple[int, int, int, int, int], tuple[int, int], int] | str:
        parts = rel.split("/")
        fds: list[int] = []
        try:
            try:
                root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                return "unreadable"
            fds.append(root_fd)
            parent_fd = root_fd
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                try:
                    info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return "missing"
                except OSError:
                    return "unreadable"
                if stat.S_ISLNK(info.st_mode):
                    return "symlink"
                if not last:
                    if not stat.S_ISDIR(info.st_mode):
                        return "unsupported"
                    try:
                        flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW
                        child = os.open(part, flags, dir_fd=parent_fd)
                    except OSError:
                        return "unreadable"
                    fds.append(child)
                    parent_fd = child
                    continue
                if not stat.S_ISREG(info.st_mode):
                    return "unsupported"
                if info.st_size > self.approval.limits.file_bytes_max:
                    return "oversized"
                if not self._charge(int(info.st_size)):
                    return "scan_limit"
                try:
                    fd = os.open(part, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
                except OSError:
                    return "unreadable"
                fds.append(fd)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
                        return "unsupported"
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        return "identity"
                    data = os.read(fd, self.approval.limits.file_bytes_max + 1)
                except OSError:
                    return "unreadable"
                if len(data) > self.approval.limits.file_bytes_max:
                    return "oversized"
                parent_info = os.fstat(parent_fd)
                identity = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_nlink,
                )
                return data, identity, (parent_info.st_dev, parent_info.st_ino), stat.S_IMODE(opened.st_mode)
            return "invalid"
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def writes_available(self) -> bool:
        return bool(_NOFOLLOW) and hasattr(os, "replace") and hasattr(os, "fchmod")

    def metadata_decision(self, path: str) -> str:
        """Name policy for a path that may already have been removed."""

        return self.authorize(path, write=False)

    def replace_file(self, snapshot: TextSnapshot, payload: bytes) -> str:
        """Atomically replace one authorized file.

        Returns ``confirmed`` or ``unconfirmed`` after the replacement, or a
        decision code when the file is left untouched. A durability failure
        after replacement returns ``unconfirmed`` and does not roll back.
        """

        if not self.writes_available():
            return self._note("unsupported")
        decision = self.authorize(snapshot.path, write=True)
        if decision != "ok":
            return decision
        lock = _path_lock(f"{self.root}\0{snapshot.path}")
        with lock:
            probe = Workspace.commit_probe
            if probe is not None:
                probe()
            return self._replace_locked(snapshot, payload)

    def _replace_locked(self, snapshot: TextSnapshot, payload: bytes) -> str:
        parts = snapshot.path.split("/")
        name = parts[-1]
        fds: list[int] = []
        committed = False
        temp_name = ""
        try:
            parent_fd, file_fd, info = self._open_parent(parts, fds)
            if isinstance(parent_fd, str):
                return self._note(parent_fd)
            assert file_fd is not None and info is not None
            try:
                current = os.read(file_fd, self.approval.limits.file_bytes_max + 1)
            except OSError:
                return self._note("unreadable")
            parent_info = os.fstat(parent_fd)
            identity = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_nlink,
            )
            if file_revision(current) != snapshot.revision:
                return self._note("stale")
            if identity != snapshot.identity or (parent_info.st_dev, parent_info.st_ino) != snapshot.parent_identity:
                return self._note("identity")
            if info.st_nlink != 1:
                return self._note("hard_link")
            if len(payload) > self.approval.limits.file_bytes_max:
                return self._note("oversized")
            temp_name = f".lw-{os.getpid()}-{token_hex(6)}.tmp"
            mode = snapshot.mode or 0o644
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    mode,
                    dir_fd=parent_fd,
                )
            except OSError:
                return self._note("unreadable")
            fds.append(temp_fd)
            try:
                # os.open applies the process umask. Restore the exact ordinary
                # bits from the snapshot instead of silently narrowing them.
                os.fchmod(temp_fd, mode)
            except OSError:
                os.close(temp_fd)
                fds.remove(temp_fd)
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = ""
                return self._note("unreadable")
            log.debug("[FIX] temp file mode bits=%s", stat.S_IMODE(mode))
            try:
                _write_all(temp_fd, payload)
                os.fsync(temp_fd)
            except OSError:
                os.close(temp_fd)
                fds.remove(temp_fd)
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = ""
                return self._note("unreadable")
            os.close(temp_fd)
            fds.remove(temp_fd)
            try:
                again = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = ""
                return self._note("identity")
            if (again.st_dev, again.st_ino, again.st_nlink) != (info.st_dev, info.st_ino, 1):
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = ""
                return self._note("identity" if again.st_nlink == 1 else "hard_link")
            try:
                os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                temp_name = ""
                return self._note("unreadable")
            committed = True
            temp_name = ""
            try:
                os.fsync(parent_fd)
            except OSError:
                log.warning("[Tools.workspace] outcome=durability")
                return "unconfirmed"
            return "confirmed"
        finally:
            if temp_name and not committed:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except (OSError, UnboundLocalError):
                    pass
            for fd in reversed(fds):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _open_parent(self, parts: list[str], fds: list[int]):
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return "unreadable", None, None
        fds.append(root_fd)
        parent_fd = root_fd
        name = parts[-1]
        for part in parts[:-1]:
            try:
                info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return "missing", None, None
            except OSError:
                return "unreadable", None, None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return "symlink" if stat.S_ISLNK(info.st_mode) else "unsupported", None, None
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
            except OSError:
                return "unreadable", None, None
            fds.append(child)
            parent_fd = child
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing", None, None
        except OSError:
            return "unreadable", None, None
        if stat.S_ISLNK(info.st_mode):
            return "symlink", None, None
        if not stat.S_ISREG(info.st_mode):
            return "unsupported", None, None
        try:
            file_fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        except OSError:
            return "unreadable", None, None
        fds.append(file_fd)
        try:
            opened = os.fstat(file_fd)
        except OSError:
            return "unreadable", None, None
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(opened.st_mode):
            return "identity", None, None
        return parent_fd, file_fd, opened


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]
