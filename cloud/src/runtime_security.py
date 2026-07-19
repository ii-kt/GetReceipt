from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from threading import RLock
from typing import BinaryIO


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_DOWNLOAD_ROOT_MARKER = ".getreceipt-ephemeral-downloads"
_INCOMPLETE_DOWNLOAD_SUFFIXES = (".crdownload", ".part", ".tmp")
_UUID_DIRECTORY = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


class RuntimeSecurityError(RuntimeError):
    pass


class WorkerInstanceLeaseError(RuntimeSecurityError):
    pass


def use_private_process_umask() -> int | None:
    """Make files subsequently created by the dedicated worker owner-only.

    Windows does not implement POSIX mode bits. Its container/host ACL remains
    the deployment platform's responsibility, so this function is a no-op
    there.
    """

    if os.name != "posix":
        return None
    return os.umask(0o077)


def ensure_private_directory(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    if not target.is_dir():
        raise RuntimeSecurityError("A private runtime directory is not a directory.")
    if os.name == "posix":
        try:
            os.chmod(target, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        except (NotImplementedError, OSError) as error:
            raise RuntimeSecurityError(
                "Could not restrict a private runtime directory."
            ) from error
    return target


def ensure_private_file(path: str | Path) -> Path:
    target = Path(path)
    if not target.exists():
        return target
    try:
        metadata = target.lstat()
    except OSError as error:
        raise RuntimeSecurityError("Could not inspect a private runtime file.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeSecurityError("A private runtime file is not a regular file.")
    if os.name == "posix":
        try:
            os.chmod(target, PRIVATE_FILE_MODE, follow_symlinks=False)
        except (NotImplementedError, OSError) as error:
            raise RuntimeSecurityError(
                "Could not restrict a private runtime file."
            ) from error
    return target


def harden_private_tree(path: str | Path) -> Path:
    """Restrict an existing profile/download tree without following symlinks."""

    root = ensure_private_directory(path)
    if os.name != "posix":
        return root
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        os.chmod(current, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        for name in directories:
            child = current / name
            if child.is_symlink():
                continue
            os.chmod(child, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        for name in files:
            child = current / name
            if child.is_symlink():
                continue
            try:
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    os.chmod(child, PRIVATE_FILE_MODE, follow_symlinks=False)
            except FileNotFoundError:
                # Chrome may have left a transient entry that disappeared while
                # walking. The worker still fails closed for other permission
                # errors.
                continue
    return root


def secure_sqlite_files(database_path: str | Path) -> None:
    path = Path(database_path)
    if str(path) == ":memory:":
        return
    ensure_private_directory(path.parent)
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        ensure_private_file(candidate)


def cleanup_stale_downloads(download_root: str | Path) -> int:
    """Remove only GetReceipt job directories and incomplete download files.

    The root is deliberately not emptied wholesale. A misconfigured path such
    as a user's ordinary Downloads folder therefore keeps unrelated content.
    """

    root = harden_private_tree(download_root)
    marker = root / _DOWNLOAD_ROOT_MARKER
    _write_private_marker(marker)
    removed = 0
    for child in tuple(root.iterdir()):
        if child == marker:
            continue
        if _UUID_DIRECTORY.fullmatch(child.name) and (
            child.is_symlink() or child.is_dir()
        ):
            if child.is_symlink():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child)
            removed += 1
            continue
        if child.is_file() and child.name.lower().endswith(
            _INCOMPLETE_DOWNLOAD_SUFFIXES
        ):
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def _write_private_marker(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        os.set_inheritable(descriptor, False)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"GetReceipt ephemeral downloads v1\n")
    finally:
        os.close(descriptor)
    ensure_private_file(path)


class WorkerInstanceLease:
    """Cross-platform, crash-safe exclusive lease for one persistent worker."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._handle: BinaryIO | None = None
        self._lock = RLock()

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        with self._lock:
            if self._handle is not None:
                return
            ensure_private_directory(self.path.parent)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(self.path, flags, PRIVATE_FILE_MODE)
            os.set_inheritable(descriptor, False)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            try:
                ensure_private_file(self.path)
                _ensure_lock_byte(handle)
                _lock_nonblocking(handle)
                _write_lease_owner(handle)
            except Exception as error:
                handle.close()
                if isinstance(error, RuntimeSecurityError):
                    raise
                raise WorkerInstanceLeaseError(
                    "Another GetReceipt worker is already using this data directory."
                ) from error
            self._handle = handle

    def release(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
            if handle is None:
                return
            try:
                _unlock(handle)
            finally:
                handle.close()

    def __enter__(self) -> WorkerInstanceLease:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\n")
        handle.flush()


def _write_lease_owner(handle: BinaryIO) -> None:
    payload = f"pid={os.getpid()}\n".encode("ascii")
    handle.seek(0)
    handle.write(payload)
    handle.truncate()
    handle.flush()
    handle.seek(0)


def _lock_nonblocking(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    raise RuntimeSecurityError("This operating system has no supported file lock.")


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
