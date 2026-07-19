from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.jobs.inbox import ChallengeResponseInbox  # noqa: E402
from src.jobs.store import SQLiteJobStore  # noqa: E402
from src.workers.runner import (  # noqa: E402
    ReceiptWorker,
    ViewerUnavailableError,
    WorkerRuntimeConfig,
)
from src.workers.service import WorkerService, WorkerServiceError  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\nviewer-frame"


class FakeBrowser:
    def __init__(self, *, profile_dir, download_dir) -> None:
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.actions: list[tuple] = []
        self.target_id = "target-1"
        self.current_url = "https://mypage.commufa.jp/auth/challenge"

    def current_page_target(self):
        return {
            "targetId": self.target_id,
            "type": "page",
            "url": self.current_url,
        }

    def screenshot_current_page(self):
        return PNG

    def click_current_page(self, x, y):
        self.actions.append(("click", x, y))

    def insert_text_current_page(self, value):
        self.actions.append(("text", value))

    def press_key_current_page(self, key):
        self.actions.append(("key", key))

    def close(self, *, clear_profile=False):
        self.actions.append(("close", clear_profile))


class InteractiveViewerTest(unittest.TestCase):
    def test_owner_operates_same_live_browser_then_worker_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteJobStore(root / "jobs.sqlite3")
            inbox = ChallengeResponseInbox()
            browsers: list[FakeBrowser] = []
            calls = 0
            acquisition_calls: list[dict] = []

            def browser_factory(**kwargs):
                browser = FakeBrowser(**kwargs)
                browsers.append(browser)
                return browser

            def acquisition_runner(**kwargs):
                nonlocal calls
                calls += 1
                acquisition_calls.append(kwargs)
                if calls == 1:
                    return SimpleNamespace(
                        success=False,
                        action_required=True,
                        challenge=SimpleNamespace(
                            kind="captcha",
                            message="本人がCAPTCHAを完了してください。",
                        ),
                        failure=None,
                    )
                return SimpleNamespace(
                    success=True,
                    action_required=False,
                    failure=None,
                )

            worker = ReceiptWorker(
                store=store,
                inbox=inbox,
                config=WorkerRuntimeConfig(
                    owner_id="owner-1",
                    profile_root=root / "profiles",
                    download_root=root / "downloads",
                    challenge_ttl_seconds=5,
                    poll_interval_seconds=0.01,
                ),
                storage_factory=lambda: object(),
                credentials_factory=lambda _service: {},
                browser_factory=browser_factory,
                fetcher_factory=lambda *_args: object(),
                acquisition_runner=acquisition_runner,
            )
            service = WorkerService(
                store=store,
                inbox=inbox,
                worker=worker,
                owner_id="owner-1",
            )
            created = service.create_job(
                owner_id="owner-1",
                target_month="2026-07",
                service_ids=["commufa"],
                idempotency_key="interactive-viewer",
            )
            thread = threading.Thread(target=worker.run_once)
            thread.start()
            waiting = self._wait(service, created["id"], "waiting_for_challenge")
            challenge = waiting["challenge"]
            self.assertEqual("captcha_interactive", challenge["kind"])
            self.assertTrue(challenge["viewer_available"])
            self.assertEqual(
                PNG,
                service.viewer_frame(
                    job_id=created["id"],
                    challenge_id=challenge["id"],
                    owner_id="owner-1",
                ),
            )
            with self.assertRaises(ViewerUnavailableError):
                worker.capture_viewer_frame(
                    created["id"],
                    "stale-challenge-id",
                )
            browsers[0].current_url = "https://example.evil/challenge"
            with self.assertRaises(WorkerServiceError) as unsafe_origin:
                service.viewer_frame(
                    job_id=created["id"],
                    challenge_id=challenge["id"],
                    owner_id="owner-1",
                )
            self.assertEqual(
                "VIEWER_UNAVAILABLE",
                unsafe_origin.exception.code,
            )
            browsers[0].current_url = "https://mypage.commufa.jp/auth/challenge"
            service.send_viewer_input(
                job_id=created["id"],
                challenge_id=challenge["id"],
                owner_id="owner-1",
                action="click",
                x=320,
                y=240,
            )
            service.send_viewer_input(
                job_id=created["id"],
                challenge_id=challenge["id"],
                owner_id="owner-1",
                action="key",
                key="Enter",
            )
            service.complete_interactive_challenge(
                job_id=created["id"],
                challenge_id=challenge["id"],
                owner_id="owner-1",
            )
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            completed = service.get_job(created["id"], owner_id="owner-1")
            self.assertEqual("succeeded", completed["state"])
            self.assertIn(("click", 320, 240), browsers[0].actions)
            self.assertIn(("key", "Enter"), browsers[0].actions)
            self.assertEqual(2, calls)
            self.assertTrue(
                all(
                    callable(call.get("cancellation_requested"))
                    for call in acquisition_calls
                )
            )
            store.close()

    def test_worker_shutdown_leaves_challenge_for_startup_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteJobStore(root / "jobs.sqlite3")
            inbox = ChallengeResponseInbox()

            def acquisition_runner(**_kwargs):
                return SimpleNamespace(
                    success=False,
                    action_required=True,
                    challenge=SimpleNamespace(
                        kind="captcha",
                        message="本人操作が必要です。",
                    ),
                    failure=None,
                )

            worker = ReceiptWorker(
                store=store,
                inbox=inbox,
                config=WorkerRuntimeConfig(
                    owner_id="owner-1",
                    profile_root=root / "profiles",
                    download_root=root / "downloads",
                    challenge_ttl_seconds=30,
                    poll_interval_seconds=0.01,
                ),
                storage_factory=lambda: object(),
                credentials_factory=lambda _service: {},
                browser_factory=FakeBrowser,
                fetcher_factory=lambda *_args: object(),
                acquisition_runner=acquisition_runner,
            )
            service = WorkerService(
                store=store,
                inbox=inbox,
                worker=worker,
                owner_id="owner-1",
            )
            created = service.create_job(
                owner_id="owner-1",
                target_month="2026-07",
                service_ids=["commufa"],
                idempotency_key="shutdown-recovery",
            )
            thread = threading.Thread(target=worker.run_once)
            thread.start()
            self._wait(service, created["id"], "waiting_for_challenge")

            worker.stop(timeout_seconds=2)
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            waiting = service.get_job(created["id"], owner_id="owner-1")
            self.assertEqual("waiting_for_challenge", waiting["state"])
            self.assertEqual(1, store.recover_incomplete_jobs(owner="owner-1"))
            recovered = service.get_job(created["id"], owner_id="owner-1")
            self.assertEqual("queued", recovered["state"])
            self.assertEqual([], recovered["failed_service_ids"])
            store.close()

    @staticmethod
    def _wait(service, job_id, state):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = service.get_job(job_id, owner_id="owner-1")
            if job["state"] == state:
                return job
            time.sleep(0.01)
        raise AssertionError(f"job did not reach {state}")


if __name__ == "__main__":
    unittest.main()
