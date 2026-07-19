from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.storage.sqlite_backup import SQLiteBackupError  # noqa: E402
from src.jobs.inbox import ChallengeResponseInbox  # noqa: E402
from src.workers.backup_lifecycle import SQLiteBackupLifecycle  # noqa: E402
from src.workers.http_api import WorkerASGIApp  # noqa: E402
from src.workers.runner import ReceiptWorker, WorkerRuntimeConfig  # noqa: E402


class SequencedBackupManager:
    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.called = threading.Condition()

    def create_backup(self):
        with self.called:
            self.calls += 1
            call_index = self.calls - 1
            self.called.notify_all()
        if call_index < len(self.outcomes):
            outcome = self.outcomes[call_index]
            if outcome is not None:
                raise outcome
        return object()

    def wait_for_calls(self, count: int, timeout: float = 2) -> bool:
        deadline = time.monotonic() + timeout
        with self.called:
            while self.calls < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.called.wait(remaining)
        return True


class FakeWorker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = False
        self.active_job_id = ""

    def start(self) -> None:
        self.events.append("worker_start")
        self.running = True

    def stop(self) -> None:
        self.events.append("worker_stop")
        self.running = False


class FakeService:
    def __init__(self, events: list[str]) -> None:
        self.worker = FakeWorker(events)

    def health(self, *, owner_id: str):
        if owner_id != "owner-1":
            raise AssertionError("owner mismatch")
        return {
            "status": "ok",
            "worker_running": self.worker.running,
        }


class FakeBackupLifecycle:
    def __init__(
        self,
        events: list[str],
        *,
        healthy: bool = True,
        fatal: bool = False,
    ) -> None:
        self.events = events
        self.healthy = healthy
        self.fatal = fatal

    def start(self) -> None:
        self.events.append("backup_start")

    def stop(self, *, create_final_backup: bool) -> bool:
        self.events.append(f"backup_stop:{create_final_backup}")
        return True

    def health(self):
        return {
            "status": "fatal" if self.fatal else "degraded",
            "last_success_at": "2026-07-19T00:00:00+00:00",
            "last_failure_at": "2026-07-19T00:01:00+00:00",
            "consecutive_failures": 3 if self.fatal else 1,
            "fatal": self.fatal,
        }


class FailingStartBackupLifecycle(FakeBackupLifecycle):
    def start(self) -> None:
        self.events.append("backup_start")
        raise SQLiteBackupError("private backup destination")


class SQLiteBackupLifecycleTest(unittest.TestCase):
    def _lifecycle(
        self,
        manager,
        callback,
        *,
        interval: float = 0.02,
        retry: float = 0.01,
        threshold: int = 2,
    ) -> SQLiteBackupLifecycle:
        return SQLiteBackupLifecycle(
            manager=manager,
            interval_seconds=interval,
            retry_interval_seconds=retry,
            fatal_failure_threshold=threshold,
            fatal_callback=callback,
        )

    def test_start_periodic_and_stop_each_publish_a_backup(self) -> None:
        manager = SequencedBackupManager()
        lifecycle = self._lifecycle(manager, lambda: None)

        lifecycle.start()
        self.assertTrue(lifecycle.healthy)
        self.assertTrue(manager.wait_for_calls(2))
        self.assertTrue(lifecycle.stop(create_final_backup=True))

        self.assertGreaterEqual(manager.calls, 3)
        self.assertEqual("stopped", lifecycle.health()["status"])

    def test_initial_backup_failure_blocks_start_without_fatal_callback(self) -> None:
        manager = SequencedBackupManager(
            [SQLiteBackupError("sensitive /private/database/path")]
        )
        callback_called = threading.Event()
        lifecycle = self._lifecycle(manager, callback_called.set, threshold=1)

        with self.assertRaisesRegex(
            SQLiteBackupError,
            "initial SQLite backup",
        ):
            lifecycle.start()

        self.assertFalse(callback_called.is_set())
        self.assertFalse(lifecycle.healthy)
        serialized = json.dumps(lifecycle.health())
        self.assertNotIn("sensitive", serialized)
        self.assertNotIn("/private/database/path", serialized)

    def test_repeated_periodic_failure_requests_one_supervisor_restart(self) -> None:
        manager = SequencedBackupManager(
            [
                None,
                SQLiteBackupError("first secret-bearing failure"),
                SQLiteBackupError("second secret-bearing failure"),
                SQLiteBackupError("must not run again"),
            ]
        )
        callback_count = 0
        callback_called = threading.Event()

        def callback() -> None:
            nonlocal callback_count
            callback_count += 1
            callback_called.set()

        lifecycle = self._lifecycle(manager, callback, threshold=2)
        lifecycle.start()
        self.assertTrue(callback_called.wait(2))

        health = lifecycle.health()
        self.assertFalse(lifecycle.healthy)
        self.assertEqual("fatal", health["status"])
        self.assertTrue(health["fatal"])
        self.assertEqual(2, health["consecutive_failures"])
        self.assertEqual(1, callback_count)
        self.assertTrue(lifecycle.stop(create_final_backup=False))
        self.assertEqual(3, manager.calls)
        self.assertNotIn("secret-bearing", json.dumps(health))

    def test_successful_retry_recovers_degraded_health(self) -> None:
        manager = SequencedBackupManager(
            [None, SQLiteBackupError("transient"), None]
        )
        callback_called = threading.Event()
        lifecycle = self._lifecycle(
            manager,
            callback_called.set,
            threshold=3,
        )
        lifecycle.start()
        self.assertTrue(manager.wait_for_calls(3))

        deadline = time.monotonic() + 1
        while not lifecycle.healthy and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertTrue(lifecycle.healthy)
        self.assertEqual(0, lifecycle.health()["consecutive_failures"])
        self.assertFalse(callback_called.is_set())
        lifecycle.stop(create_final_backup=False)


class CrashingStore:
    def recover_incomplete_jobs(self, *, owner: str) -> None:
        if owner != "owner-1":
            raise AssertionError("owner mismatch")

    def claim_next(self, *, owner: str):
        raise RuntimeError("top-secret database path")


class WorkerFatalExitTest(unittest.TestCase):
    def test_unexpected_worker_loop_exit_requests_supervisor_restart(self) -> None:
        callback_called = threading.Event()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ReceiptWorker(
                store=CrashingStore(),  # type: ignore[arg-type]
                inbox=ChallengeResponseInbox(),
                config=WorkerRuntimeConfig(
                    owner_id="owner-1",
                    profile_root=root / "profiles",
                    download_root=root / "downloads",
                    poll_interval_seconds=0.01,
                ),
                storage_factory=lambda: object(),
                credentials_factory=lambda _service_id: {},
                fatal_callback=callback_called.set,
            )
            with self.assertLogs(
                "src.workers.runner",
                level="CRITICAL",
            ) as captured:
                worker.start()
                self.assertTrue(callback_called.wait(2))
                worker.stop()

        logs = "\n".join(captured.output)
        self.assertIn("RuntimeError", logs)
        self.assertNotIn("top-secret", logs)
        self.assertFalse(worker.running)


class WorkerBackupASGIIntegrationTest(unittest.TestCase):
    def test_initial_backup_failure_prevents_worker_start_and_closes_database(
        self,
    ) -> None:
        events: list[str] = []
        lifecycle = FailingStartBackupLifecycle(events)
        app = WorkerASGIApp(
            service=FakeService(events),
            api_token="t" * 48,
            backup_lifecycle=lifecycle,
            shutdown_callback=lambda: events.append("database_close"),
        )

        async def run() -> list[dict]:
            sent: list[dict] = []

            async def receive():
                return {"type": "lifespan.startup"}

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            return sent

        sent = asyncio.run(run())

        self.assertEqual(
            ["backup_start", "backup_stop:False", "database_close"],
            events,
        )
        self.assertEqual("lifespan.startup.failed", sent[0]["type"])
        self.assertNotIn("private backup destination", sent[0]["message"])

    def test_lifespan_orders_initial_and_final_backup_around_worker(self) -> None:
        events: list[str] = []
        lifecycle = FakeBackupLifecycle(events)
        app = WorkerASGIApp(
            service=FakeService(events),
            api_token="t" * 48,
            backup_lifecycle=lifecycle,
            shutdown_callback=lambda: events.append("database_close"),
        )

        async def run() -> list[dict]:
            incoming = [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
            sent: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            return sent

        sent = asyncio.run(run())

        self.assertEqual(
            [
                "backup_start",
                "worker_start",
                "worker_stop",
                "backup_stop:True",
                "database_close",
            ],
            events,
        )
        self.assertEqual(
            ["lifespan.startup.complete", "lifespan.shutdown.complete"],
            [message["type"] for message in sent],
        )

    def test_lifespan_does_not_close_database_while_worker_is_still_alive(self) -> None:
        events: list[str] = []
        lifecycle = FakeBackupLifecycle(events)
        service = FakeService(events)

        def failed_stop() -> bool:
            events.append("worker_stop_timeout")
            return False

        service.worker.stop = failed_stop  # type: ignore[method-assign]
        app = WorkerASGIApp(
            service=service,
            api_token="t" * 48,
            backup_lifecycle=lifecycle,
            shutdown_callback=lambda: events.append("database_close"),
        )

        async def run() -> list[dict]:
            incoming = [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
            sent: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            return sent

        with self.assertLogs("src.workers.http_api", level="CRITICAL") as captured:
            sent = asyncio.run(run())

        self.assertEqual(
            [
                "backup_start",
                "worker_start",
                "worker_stop_timeout",
                "backup_stop:True",
            ],
            events,
        )
        self.assertNotIn("database_close", events)
        self.assertIn("intentionally skipped", "\n".join(captured.output))
        self.assertEqual("lifespan.shutdown.complete", sent[-1]["type"])

    def test_unhealthy_backup_returns_503_without_internal_details(self) -> None:
        events: list[str] = []
        lifecycle = FakeBackupLifecycle(events, healthy=False, fatal=True)
        app = WorkerASGIApp(
            service=FakeService(events),
            api_token="t" * 48,
            backup_lifecycle=lifecycle,
        )

        async def run() -> tuple[int, dict]:
            incoming = [
                {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            ]
            sent: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                sent.append(message)

            await app(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/healthz",
                    "query_string": b"",
                    "headers": [
                        (b"authorization", f"Bearer {'t' * 48}".encode()),
                        (b"x-getreceipt-owner", b"owner-1"),
                    ],
                },
                receive,
                send,
            )
            return (
                sent[0]["status"],
                json.loads(sent[1]["body"].decode("utf-8")),
            )

        status, payload = asyncio.run(run())

        self.assertEqual(503, status)
        self.assertEqual("fatal", payload["status"])
        self.assertTrue(payload["sqlite_backup"]["fatal"])
        self.assertNotIn("path", json.dumps(payload).lower())
        self.assertNotIn("exception", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
