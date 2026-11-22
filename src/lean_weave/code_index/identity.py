"""Checkout identity and the private, disposable persistent-index location.

All paths here are opened without following links. Construction is
side-effect free; directories are only created by an explicit writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lean_weave.errors import CodeIndexError
from lean_weave.logging_setup import get_logger

log = get_logger("code_index.identity")

APP_DIR = "lean-weave"
CACHE_DIR = "code-index"
CONTEXT_CACHE_DIR = "context-cache"
CONTEXT_DB_NAME = "cache.sqlite3"
CONTEXT_LOCK_NAME = "cache.lock"
CONTEXT_PENDING_NAME = "cache.pending.sqlite3"
DB_NAME = "index.sqlite3"
LOCK_NAME = "index.lock"
PENDING_NAME = "index.pending.sqlite3"
_LEAF_NAMES = (
    DB_NAME,
    LOCK_NAME,
    PENDING_NAME,
    f"{DB_NAME}-journal",
    f"{PENDING_NAME}-journal",
    f"{DB_NAME}-wal",
    f"{DB_NAME}-shm",
    f"{PENDING_NAME}-wal",
    f"{PENDING_NAME}-shm",
)


@dataclass(frozen=True)
class CheckoutIdentity:
    key: str


@dataclass(frozen=True)
class IndexLocation:
    checkout_key: str
    cache_base: Path
    checkout_dir: Path

    @property
    def database(self) -> Path:
        return self.checkout_dir / DB_NAME

    @property
    def lock(self) -> Path:
        return self.checkout_dir / LOCK_NAME

    @property
    def pending(self) -> Path:
        return self.checkout_dir / PENDING_NAME

    @property
    def database_journal(self) -> Path:
        return self.checkout_dir / f"{DB_NAME}-journal"

    @property
    def pending_journal(self) -> Path:
        return self.checkout_dir / f"{PENDING_NAME}-journal"

    @property
    def leaves(self) -> dict[str, Path]:
        return {name: self.checkout_dir / name for name in _LEAF_NAMES}

    @property
    def unsafe_sidecars(self) -> tuple[Path, ...]:
        return tuple(
            self.checkout_dir / name
            for name in (
                f"{DB_NAME}-wal",
                f"{DB_NAME}-shm",
                f"{PENDING_NAME}-wal",
                f"{PENDING_NAME}-shm",
            )
        )


@dataclass(frozen=True)
class CacheLocation:
    """Fixed private leaf set for one checkout's context cache."""

    checkout_key: str
    cache_base: Path
    checkout_dir: Path

    @property
    def database(self) -> Path:
        return self.checkout_dir / CONTEXT_DB_NAME

    @property
    def lock(self) -> Path:
        return self.checkout_dir / CONTEXT_LOCK_NAME

    @property
    def pending(self) -> Path:
        return self.checkout_dir / CONTEXT_PENDING_NAME

    @property
    def database_journal(self) -> Path:
        return self.checkout_dir / f"{CONTEXT_DB_NAME}-journal"

    @property
    def pending_journal(self) -> Path:
        return self.checkout_dir / f"{CONTEXT_PENDING_NAME}-journal"

    @property
    def leaves(self) -> dict[str, Path]:
        return {
            CONTEXT_DB_NAME: self.database,
            CONTEXT_LOCK_NAME: self.lock,
            CONTEXT_PENDING_NAME: self.pending,
            f"{CONTEXT_DB_NAME}-journal": self.database_journal,
            f"{CONTEXT_PENDING_NAME}-journal": self.pending_journal,
        }

    @property
    def unsafe_sidecars(self) -> tuple[Path, ...]:
        return (
            self.checkout_dir / f"{CONTEXT_DB_NAME}-wal",
            self.checkout_dir / f"{CONTEXT_DB_NAME}-shm",
            self.checkout_dir / f"{CONTEXT_PENDING_NAME}-wal",
            self.checkout_dir / f"{CONTEXT_PENDING_NAME}-shm",
        )


def resolve_checkout(root: Path) -> CheckoutIdentity:
    """Bind the cache namespace to the real directory identity, not Git."""

    try:
        resolved = Path(root).resolve(strict=True)
        info = os.stat(resolved)
    except (OSError, RuntimeError, ValueError):
        raise CodeIndexError("root_unavailable") from None
    if not stat.S_ISDIR(info.st_mode):
        raise CodeIndexError("root_unavailable")
    payload = json.dumps(
        {"path": str(resolved), "dev": info.st_dev, "ino": info.st_ino},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key = hashlib.sha256(payload).hexdigest()
    return CheckoutIdentity(key)


def _cache_base(cache_base: Path | None) -> Path:
    if cache_base is not None:
        base = Path(cache_base)
        if not base.is_absolute():
            raise CodeIndexError("unsafe_cache")
        return base
    selected = os.environ.get("XDG_CACHE_HOME")
    if selected:
        base = Path(selected)
        if not base.is_absolute():
            raise CodeIndexError("unsafe_cache")
        return base
    return Path.home() / ".cache"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False


def _mode_ok(info: os.stat_result, *, owner_only: bool) -> bool:
    if info.st_uid not in {0, os.geteuid()}:
        return False
    if owner_only:
        return info.st_mode & 0o077 == 0
    if info.st_mode & 0o022:
        # A sticky directory owned by root is acceptable as an intermediate.
        return bool(info.st_mode & stat.S_ISVTX) and info.st_uid == 0
    return True


def _check_ancestors(path: Path, *, owner_only_tail: int = 0) -> None:
    """Validate every existing component without following links."""

    try:
        parts = path.parts
    except (OSError, ValueError):
        raise CodeIndexError("unsafe_cache") from None
    current = Path(parts[0])
    tail = set(path.parts[-owner_only_tail:]) if owner_only_tail else set()
    directories = [current, *[current.joinpath(*parts[1 : index + 1]) for index in range(1, len(parts))]]
    for directory in directories:
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            break
        except OSError:
            raise CodeIndexError("unsafe_cache") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CodeIndexError("unsafe_cache")
        required_owner_only = str(directory) in tail or directory.name in tail
        if not _mode_ok(info, owner_only=required_owner_only):
            raise CodeIndexError("unsafe_cache")


def _ensure_private_dir(path: Path) -> None:
    _check_ancestors(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        pass
    except OSError:
        raise CodeIndexError("storage_unavailable") from None
    try:
        info = os.lstat(path)
    except OSError:
        raise CodeIndexError("storage_unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CodeIndexError("unsafe_cache")
    if not _mode_ok(info, owner_only=True):
        raise CodeIndexError("unsafe_cache")


def index_location(
    identity: CheckoutIdentity,
    *,
    cache_base: Path | None = None,
    source_root: Path | None = None,
) -> IndexLocation:
    """Resolve the fixed cache location without creating or opening anything."""

    if not identity.key or len(identity.key) != 64 or any(
        char not in "0123456789abcdef" for char in identity.key
    ):
        raise CodeIndexError("unsafe_cache")
    base = _cache_base(cache_base)
    checkout = base / APP_DIR / CACHE_DIR / identity.key
    if source_root is not None and _is_within(checkout, source_root):
        raise CodeIndexError("unsafe_cache")
    _check_ancestors(checkout)
    return IndexLocation(identity.key, base, checkout)


def context_cache_location(
    identity: CheckoutIdentity,
    *,
    cache_base: Path | None = None,
    source_root: Path | None = None,
) -> CacheLocation:
    """Resolve the fixed context-cache location without creating anything."""

    if not identity.key or len(identity.key) != 64 or any(
        char not in "0123456789abcdef" for char in identity.key
    ):
        raise CodeIndexError("unsafe_cache")
    base = _cache_base(cache_base)
    checkout = base / APP_DIR / CONTEXT_CACHE_DIR / identity.key
    if source_root is not None and _is_within(checkout, source_root):
        raise CodeIndexError("unsafe_cache")
    _check_ancestors(checkout)
    return CacheLocation(identity.key, base, checkout)


def ensure_cache_location(location: CacheLocation) -> None:
    """Create and validate the private context-cache directory tree."""

    base = location.cache_base
    try:
        os.lstat(base)
    except FileNotFoundError:
        try:
            base.mkdir(mode=0o700, parents=True)
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
    except OSError:
        raise CodeIndexError("unsafe_cache") from None
    _check_ancestors(base)
    _ensure_private_dir(base / APP_DIR)
    _ensure_private_dir(base / APP_DIR / CONTEXT_CACHE_DIR)
    _ensure_private_dir(location.checkout_dir)
    _check_ancestors(location.checkout_dir)


def ensure_location(location: IndexLocation) -> None:
    """Create and validate the private cache directory tree for a writer."""

    base = location.cache_base
    try:
        os.lstat(base)
    except FileNotFoundError:
        try:
            base.mkdir(mode=0o700, parents=True)
        except OSError:
            raise CodeIndexError("storage_unavailable") from None
    except OSError:
        raise CodeIndexError("unsafe_cache") from None
    _check_ancestors(base)
    _ensure_private_dir(base / APP_DIR)
    _ensure_private_dir(base / APP_DIR / CACHE_DIR)
    _ensure_private_dir(location.checkout_dir)
    _check_ancestors(location.checkout_dir)


def open_lock(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        raise CodeIndexError("missing") from None
    except OSError:
        raise CodeIndexError("storage_unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CodeIndexError("unsafe_cache")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise CodeIndexError("unsafe_cache")
    except CodeIndexError:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise CodeIndexError("storage_unavailable") from None
    return fd


def describe_leaf(path: Path) -> str:
    """Classify one fixed leaf without following links."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    if stat.S_ISLNK(info.st_mode):
        return "unsafe"
    if not stat.S_ISREG(info.st_mode):
        return "unsafe"
    return "present"
