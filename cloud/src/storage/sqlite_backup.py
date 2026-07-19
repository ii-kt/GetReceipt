from __future__ import annotations

import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from ..runtime_security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    ensure_private_file,
)


_BACKUP_DIRECTORY_NAME = ".sqlite-backups"
_BACKUP_FILENAME = re.compile(
    r"^getreceipt-sqlite-"
    r"\d{8}T\d{12}Z-"
    r"[0-9a-f]{32}\.sqlite3$"
)
_TEMPORARY_FILENAME = re.compile(
    r"^\.getreceipt-sqlite-[0-9a-f]{32}\.tmp$"
)


class SQLiteBackupError(RuntimeError):
    """A safe, path-free error raised when an online backup cannot complete."""


@dataclass(frozen=True)
class SQLiteBackupResult:
    path: Path
    pruned_count: int


class SQLiteBackupManager:
    """Create private, consistent SQLite snapshots inside one persistent root.

    A manager instance is safe to call from a periodic thread while the live
    database remains open. SQLite's online backup API owns snapshot
    consistency; the final rename happens only after the copied database passes
    ``PRAGMA quick_check`` and has been flushed.
    """

    def __init__(
        self,
        *,
        database_path: str | Path,
        persistent_root: str | Path,
        retention_count: int = 7,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(retention_count, bool) or retention_count < 1:
            raise ValueError("retention_count must be a positive integer")
        self.database_path = Path(database_path)
        self.persistent_root = Path(persistent_root)
        self.retention_count = int(retention_count)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    @property
    def backup_directory(self) -> Path:
        return self.persistent_root / _BACKUP_DIRECTORY_NAME

    def create_backup(self) -> SQLiteBackupResult:
        """Create one online snapshot, atomically publish it, then prune."""

        with self._lock:
            try:
                root = _private_directory(self.persistent_root)
                backup_directory = _private_directory(
                    root / _BACKUP_DIRECTORY_NAME
                )
                _require_within(backup_directory, root)
                database = _live_database(
                    self.database_path,
                    root=root,
                    backup_directory=backup_directory,
                )
                timestamp = _timestamp(self._clock())
                temporary_path, final_path = _allocate_paths(
                    backup_directory,
                    timestamp=timestamp,
                )
            except (OSError, sqlite3.Error, ValueError):
                raise SQLiteBackupError(
                    "The SQLite backup could not be prepared."
                ) from None

            published = False
            try:
                _copy_online(database, temporary_path)
                ensure_private_file(temporary_path)
                _sync_file(temporary_path)
                if final_path.exists() or final_path.is_symlink():
                    raise SQLiteBackupError(
                        "The SQLite backup destination is unavailable."
                    )
                os.replace(temporary_path, final_path)
                published = True
                ensure_private_file(final_path)
                _sync_directory(backup_directory)
                pruned_count = self._prune(
                    backup_directory,
                    protected=final_path,
                )
                return SQLiteBackupResult(
                    path=final_path,
                    pruned_count=pruned_count,
                )
            except SQLiteBackupError:
                raise
            except (OSError, sqlite3.Error):
                raise SQLiteBackupError(
                    "The SQLite backup could not be completed."
                ) from None
            finally:
                if not published:
                    _remove_owned_temporary(temporary_path, backup_directory)

    def _prune(self, backup_directory: Path, *, protected: Path) -> int:
        protected_resolved = protected.resolve(strict=True)
        _require_within(protected_resolved, backup_directory)
        candidates: list[Path] = []
        for candidate in backup_directory.iterdir():
            if not _BACKUP_FILENAME.fullmatch(candidate.name):
                continue
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                continue
            resolved = candidate.resolve(strict=True)
            _require_within(resolved, backup_directory)
            candidates.append(candidate)

        candidates.sort(key=lambda path: path.name, reverse=True)
        kept = {protected_resolved}
        for candidate in candidates:
            if len(kept) >= self.retention_count:
                break
            kept.add(candidate.resolve(strict=True))

        removed = 0
        for candidate in candidates:
            resolved = candidate.resolve(strict=True)
            if resolved in kept:
                continue
            _require_within(resolved, backup_directory)
            candidate.unlink()
            removed += 1
        if removed:
            _sync_directory(backup_directory)
        return removed


def _private_directory(path: Path) -> Path:
    target = Path(path)
    if target.is_symlink():
        raise SQLiteBackupError("A private backup directory is unavailable.")
    ensure_private_directory(target)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SQLiteBackupError("A private backup directory is unavailable.")
    resolved = target.resolve(strict=True)
    ensure_private_directory(resolved)
    return resolved


def _live_database(
    path: Path,
    *,
    root: Path,
    backup_directory: Path,
) -> Path:
    source = Path(path)
    if source.is_symlink():
        raise SQLiteBackupError("The SQLite backup source is unavailable.")
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SQLiteBackupError("The SQLite backup source is unavailable.")
    resolved = source.resolve(strict=True)
    _require_within(resolved, root)
    try:
        resolved.relative_to(backup_directory)
    except ValueError:
        pass
    else:
        raise SQLiteBackupError("The SQLite backup source is unavailable.")
    ensure_private_file(resolved)
    return resolved


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        raise SQLiteBackupError(
            "A SQLite backup path is outside the persistent root."
        ) from None


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("backup clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _allocate_paths(
    backup_directory: Path,
    *,
    timestamp: str,
) -> tuple[Path, Path]:
    for _attempt in range(10):
        nonce = uuid4().hex
        temporary = backup_directory / f".getreceipt-sqlite-{nonce}.tmp"
        final = (
            backup_directory
            / f"getreceipt-sqlite-{timestamp}-{nonce}.sqlite3"
        )
        if final.exists() or final.is_symlink():
            continue
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(temporary, flags, PRIVATE_FILE_MODE)
        except FileExistsError:
            continue
        try:
            os.set_inheritable(descriptor, False)
        finally:
            os.close(descriptor)
        ensure_private_file(temporary)
        return temporary, final
    raise SQLiteBackupError("A unique SQLite backup name could not be allocated.")


def _copy_online(database: Path, temporary_path: Path) -> None:
    source_uri = f"{database.as_uri()}?mode=ro"
    with closing(
        sqlite3.connect(source_uri, uri=True, timeout=30)
    ) as source, closing(
        sqlite3.connect(temporary_path, timeout=30)
    ) as destination:
        source.backup(destination)
        check_rows = destination.execute("PRAGMA quick_check").fetchall()
        if check_rows != [("ok",)]:
            raise SQLiteBackupError(
                "The SQLite backup did not pass its integrity check."
            )


def _sync_file(path: Path) -> None:
    # Windows requires a writable descriptor for fsync/FlushFileBuffers even
    # though this operation does not modify the already completed snapshot.
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some otherwise safe filesystems do not implement directory fsync.
        pass
    finally:
        os.close(descriptor)


def _remove_owned_temporary(path: Path, backup_directory: Path) -> None:
    if not _TEMPORARY_FILENAME.fullmatch(path.name):
        return
    try:
        parent = path.parent.resolve(strict=True)
        _require_within(parent, backup_directory)
    except (OSError, SQLiteBackupError):
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
