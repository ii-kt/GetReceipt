from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event, RLock, Thread, current_thread
from typing import Any

from ..storage.sqlite_backup import SQLiteBackupError, SQLiteBackupManager


LOGGER = logging.getLogger(__name__)


class SQLiteBackupLifecycle:
    """Own startup, periodic, and shutdown snapshots for one worker process.

    The initial snapshot is a startup gate: acquisition does not begin unless
    the persistent database can be read and a verified backup can be published.
    A periodic failure makes health checks fail immediately. Repeated failures
    invoke the injected fatal callback once so the process supervisor can
    restart the worker; exception messages and filesystem paths never enter the
    public status payload or log message.
    """

    def __init__(
        self,
        *,
        manager: SQLiteBackupManager,
        interval_seconds: float,
        retry_interval_seconds: float,
        fatal_failure_threshold: int,
        fatal_callback: Callable[[], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0 or retry_interval_seconds <= 0:
            raise ValueError("backup intervals must be positive")
        if (
            isinstance(fatal_failure_threshold, bool)
            or fatal_failure_threshold < 1
        ):
            raise ValueError("fatal_failure_threshold must be a positive integer")
        if not callable(fatal_callback):
            raise TypeError("fatal_callback must be callable")
        self.manager = manager
        self.interval_seconds = float(interval_seconds)
        self.retry_interval_seconds = float(retry_interval_seconds)
        self.fatal_failure_threshold = int(fatal_failure_threshold)
        self._fatal_callback = fatal_callback
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop_event = Event()
        self._lock = RLock()
        self._thread: Thread | None = None
        self._started = False
        self._stopping = False
        self._stopped = False
        self._fatal = False
        self._fatal_notified = False
        self._consecutive_failures = 0
        self._last_success_at = ""
        self._last_failure_at = ""

    @property
    def healthy(self) -> bool:
        with self._lock:
            return bool(
                self._started
                and not self._stopping
                and not self._fatal
                and self._consecutive_failures == 0
                and self._last_success_at
            )

    def health(self) -> dict[str, Any]:
        """Return only non-sensitive operational state for ``/healthz``."""

        with self._lock:
            if self._fatal:
                status = "fatal"
            elif self._stopping:
                status = "stopping"
            elif self._consecutive_failures:
                status = "degraded"
            elif self._stopped:
                status = "stopped"
            elif self._started and self._last_success_at:
                status = "ok"
            else:
                status = "starting"
            return {
                "status": status,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "consecutive_failures": self._consecutive_failures,
                "fatal": self._fatal,
            }

    def start(self) -> None:
        """Require one verified backup, then start periodic snapshots."""

        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            self._stopping = False
            self._stopped = False

        if not self._backup_once(allow_fatal_callback=False):
            raise SQLiteBackupError(
                "The initial SQLite backup could not be completed."
            )

        with self._lock:
            self._started = True
            thread = Thread(
                target=self._run,
                name="getreceipt-sqlite-backup",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._thread = None
                self._started = False
            raise

    def stop(
        self,
        *,
        create_final_backup: bool = True,
        timeout_seconds: float = 35,
    ) -> bool:
        """Stop periodic work and, when possible, publish a final snapshot."""

        with self._lock:
            was_started = self._started
            self._stopping = True
            self._stop_event.set()
            thread = self._thread

        if thread is not None and thread is not current_thread():
            thread.join(timeout=max(0.1, float(timeout_seconds)))
        thread_stopped = thread is None or not thread.is_alive()

        final_ok = True
        if create_final_backup and was_started and thread_stopped:
            final_ok = self._backup_once(allow_fatal_callback=False)
        elif create_final_backup and was_started and not thread_stopped:
            final_ok = False
            LOGGER.error(
                "SQLite backup thread did not stop before the shutdown deadline"
            )

        with self._lock:
            if thread_stopped:
                self._thread = None
                self._started = False
            self._stopping = False
            self._stopped = thread_stopped
        return bool(thread_stopped and final_ok)

    def _run(self) -> None:
        delay = self.interval_seconds
        while not self._stop_event.wait(delay):
            succeeded = self._backup_once(allow_fatal_callback=True)
            with self._lock:
                fatal = self._fatal
            if fatal:
                return
            delay = (
                self.interval_seconds
                if succeeded
                else self.retry_interval_seconds
            )

    def _backup_once(self, *, allow_fatal_callback: bool) -> bool:
        try:
            self.manager.create_backup()
        except Exception as error:
            LOGGER.warning(
                "SQLite backup attempt failed (%s)",
                type(error).__name__,
            )
            should_notify = False
            with self._lock:
                self._consecutive_failures += 1
                self._last_failure_at = self._now_text()
                if (
                    allow_fatal_callback
                    and self._consecutive_failures
                    >= self.fatal_failure_threshold
                ):
                    self._fatal = True
                    if not self._fatal_notified:
                        self._fatal_notified = True
                        should_notify = True
            if should_notify:
                try:
                    self._fatal_callback()
                except Exception as callback_error:
                    LOGGER.error(
                        "SQLite backup fatal callback failed (%s)",
                        type(callback_error).__name__,
                    )
            return False

        with self._lock:
            self._consecutive_failures = 0
            self._last_success_at = self._now_text()
        return True

    def _now_text(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("backup lifecycle clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
