from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.jobs.inbox import ChallengeResponseInbox  # noqa: E402
from src.jobs.models import BatchJobState, Challenge, ChallengeType  # noqa: E402
from src.jobs.store import SQLiteJobStore, VersionConflictError  # noqa: E402
from src.domain.acquisition import (  # noqa: E402
    AcquisitionOutcome,
    StoredReceiptReference,
)
from src.workers.runner import ReceiptWorker, WorkerRuntimeConfig  # noqa: E402
from src.workers.service import WorkerService, WorkerServiceError  # noqa: E402
from src.workflows.auto_acquisition import run_auto_acquisition  # noqa: E402


class FakeBrowser:
    instances: list["FakeBrowser"] = []

    def __init__(self, *, profile_dir: Path, download_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.closed_with: list[bool] = []
        self.__class__.instances.append(self)

    def close(self, *, clear_profile: bool = False) -> None:
        self.closed_with.append(clear_profile)


class FakeFetcher:
    def __init__(self) -> None:
        self.resume_calls: list[tuple[str, str]] = []

    def fetch_pdf(self, target_month: str):
        raise AssertionError("fake acquisition runner owns the result")

    def resume_after_security_code(self, target_month: str, code: str):
        self.resume_calls.append((target_month, code))
        return SimpleNamespace(content=b"%PDF fake")


class WorkerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeBrowser.instances.clear()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteJobStore(root / "jobs.sqlite3")
        self.inbox = ChallengeResponseInbox()
        self.fetcher = FakeFetcher()
        self.acquisition_calls: list[dict] = []

        def acquisition_runner(**kwargs):
            self.acquisition_calls.append(kwargs)
            if "fetch_statement" not in kwargs:
                return SimpleNamespace(
                    success=False,
                    action_required=True,
                    challenge=SimpleNamespace(
                        kind="verification_code",
                        message="メール確認コードを入力してください。",
                    ),
                    failure=None,
                )
            statement = kwargs["fetch_statement"](kwargs["target_month"])
            self.assertTrue(statement.content.startswith(b"%PDF"))
            return SimpleNamespace(
                success=True,
                action_required=False,
                failure=None,
            )

        self.worker = ReceiptWorker(
            store=self.store,
            inbox=self.inbox,
            config=WorkerRuntimeConfig(
                owner_id="owner-1",
                profile_root=root / "profiles",
                download_root=root / "downloads",
                challenge_ttl_seconds=5,
                poll_interval_seconds=0.01,
            ),
            storage_factory=lambda: object(),
            credentials_factory=lambda _service_id: {
                "login_id": "configured",
                "password": "configured",
            },
            browser_factory=FakeBrowser,
            fetcher_factory=lambda *_args: self.fetcher,
            acquisition_runner=acquisition_runner,
        )
        self.service = WorkerService(
            store=self.store,
            inbox=self.inbox,
            worker=self.worker,
            owner_id="owner-1",
        )

    def tearDown(self) -> None:
        self.worker.stop(timeout_seconds=1)
        self.store.close()
        self.temp.cleanup()

    def test_fake_end_to_end_mobile_reload_and_one_time_challenge(self) -> None:
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["commufa"],
            idempotency_key="mobile-contract",
        )
        worker_thread = threading.Thread(target=self.worker.run_once)
        worker_thread.start()

        waiting = self._wait_for_state(created["id"], "waiting_for_challenge")
        challenge = waiting["challenge"]
        self.assertEqual("otp_email", challenge["kind"])
        self.assertEqual(6, challenge["input_schema"]["max_length"])

        # A completely new service/controller can recover the durable job.
        reloaded_service = WorkerService(
            store=self.store,
            inbox=self.inbox,
            worker=self.worker,
            owner_id="owner-1",
        )
        recovered = reloaded_service.get_job(created["id"], owner_id="owner-1")
        self.assertEqual(waiting["id"], recovered["id"])
        self.assertEqual(challenge["id"], recovered["challenge"]["id"])

        reloaded_service.submit_challenge_response(
            job_id=created["id"],
            challenge_id=challenge["id"],
            owner_id="owner-1",
            response="１２３４５６",
        )
        with self.assertRaises(WorkerServiceError) as duplicate:
            reloaded_service.submit_challenge_response(
                job_id=created["id"],
                challenge_id=challenge["id"],
                owner_id="owner-1",
                response="999999",
            )
        self.assertIn(duplicate.exception.code, {"CHALLENGE_ALREADY_ANSWERED", "CHALLENGE_NOT_PENDING"})

        worker_thread.join(timeout=5)
        self.assertFalse(worker_thread.is_alive())
        completed = self.service.get_job(created["id"], owner_id="owner-1")
        self.assertEqual("succeeded", completed["state"])
        self.assertEqual(["commufa"], completed["completed_service_ids"])
        self.assertIsNone(completed["challenge"])
        self.assertEqual([("2026-07", "123456")], self.fetcher.resume_calls)
        self.assertEqual([False], FakeBrowser.instances[0].closed_with)
        self.assertEqual(2, len(self.acquisition_calls))
        self.assertTrue(
            all(callable(call.get("cancellation_requested")) for call in self.acquisition_calls)
        )
        self.assertTrue(
            all(not call["cancellation_requested"]() for call in self.acquisition_calls)
        )

        with closing(sqlite3.connect(self.store.path)) as connection:
            rows = "\n".join(
                str(value)
                for row in connection.execute(
                    "SELECT challenge_json FROM challenges"
                ).fetchall()
                for value in row
            )
            events = "\n".join(
                str(value)
                for row in connection.execute(
                    "SELECT payload_json FROM job_events"
                ).fetchall()
                for value in row
            )
        self.assertNotIn("123456", rows)
        self.assertNotIn("123456", events)

    def test_manual_upload_runs_in_worker_storage_and_returns_sanitized_result(self) -> None:
        storage_calls: list[tuple[str, bytes, str]] = []

        class Storage:
            def list_files(self):
                return []

            def upsert_bytes(self, *, file_name, content, mime_type):
                storage_calls.append((file_name, content, mime_type))
                return {"id": "drive-1"}

        def manual_workflow(**kwargs):
            kwargs["storage"].list_files()
            kwargs["storage"].upsert_bytes(
                file_name="receipt.pdf",
                content=kwargs["content"],
                mime_type="application/pdf",
            )
            return SimpleNamespace(
                success=True,
                outcome=AcquisitionOutcome.ACQUIRED,
                receipt=StoredReceiptReference(
                    file_id="drive-1",
                    file_name="receipt.pdf",
                    web_view_link="https://drive.google.com/drive-1",
                ),
            )

        self.worker.storage_factory = Storage
        with patch(
            "src.workers.service.save_manual_receipt_workflow",
            side_effect=manual_workflow,
        ):
            payload = self.service.save_manual_receipt(
                owner_id="owner-1",
                service_id="epos",
                target_month="2026-07",
                content=b"%PDF private statement",
                confirmed=True,
            )

        self.assertTrue(payload["success"])
        self.assertEqual("acquired", payload["status"])
        self.assertEqual("drive-1", payload["receipt"]["file_id"])
        self.assertEqual(
            [("receipt.pdf", b"%PDF private statement", "application/pdf")],
            storage_calls,
        )
        self.assertNotIn("private statement", str(payload))

    def test_manual_upload_rejects_active_job_and_wrong_owner(self) -> None:
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos"],
            idempotency_key="manual-upload-conflict",
        )
        self.assertEqual("queued", created["state"])
        for owner_id, expected_code in (
            ("owner-2", "OWNER_FORBIDDEN"),
            ("owner-1", "MANUAL_UPLOAD_BUSY"),
        ):
            with self.subTest(owner_id=owner_id):
                with self.assertRaises(WorkerServiceError) as raised:
                    self.service.save_manual_receipt(
                        owner_id=owner_id,
                        service_id="epos",
                        target_month="2026-07",
                        content=b"%PDF private statement",
                        confirmed=False,
                    )
                self.assertEqual(expected_code, raised.exception.code)

    def test_owner_mismatch_and_invalid_code_are_rejected(self) -> None:
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["commufa"],
            idempotency_key="owner-check",
        )
        worker_thread = threading.Thread(target=self.worker.run_once)
        worker_thread.start()
        waiting = self._wait_for_state(created["id"], "waiting_for_challenge")

        with self.assertRaises(WorkerServiceError) as forbidden:
            self.service.get_job(created["id"], owner_id="owner-2")
        self.assertEqual("OWNER_FORBIDDEN", forbidden.exception.code)

        with self.assertRaises(WorkerServiceError) as invalid:
            self.service.submit_challenge_response(
                job_id=created["id"],
                challenge_id=waiting["challenge"]["id"],
                owner_id="owner-1",
                response="ABCDEF",
            )
        self.assertEqual("CHALLENGE_RESPONSE_INVALID", invalid.exception.code)

        self.service.cancel_job(created["id"], owner_id="owner-1")
        worker_thread.join(timeout=5)
        self.assertEqual(
            "cancelled",
            self.service.get_job(created["id"], owner_id="owner-1")["state"],
        )

    def test_duplicate_create_is_idempotent(self) -> None:
        values = dict(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos"],
            idempotency_key="same-tap",
        )
        first = self.service.create_job(**values)
        second = self.service.create_job(**values)
        self.assertEqual(first["id"], second["id"])

    def test_provider_failure_is_recorded_and_remaining_service_continues(self) -> None:
        calls: list[str] = []

        def partial_runner(**kwargs):
            service_id = kwargs["service_id"]
            calls.append(service_id)
            if service_id == "epos":
                return SimpleNamespace(
                    success=False,
                    action_required=False,
                    failure=SimpleNamespace(
                        code="PDF_SIGNATURE_MISSING",
                        message="provider payload secret=should-not-persist",
                    ),
                )
            return SimpleNamespace(
                success=True,
                action_required=False,
                failure=None,
            )

        self.worker.acquisition_runner = partial_runner
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos", "commufa"],
            idempotency_key="partial-success",
        )

        self.assertTrue(self.worker.run_once())
        completed = self.service.get_job(created["id"], owner_id="owner-1")

        self.assertEqual(["epos", "commufa"], calls)
        self.assertEqual("failed", completed["state"])
        self.assertEqual(["commufa"], completed["completed_service_ids"])
        self.assertEqual(["epos"], completed["failed_service_ids"])
        self.assertEqual(
            "PDF_SIGNATURE_MISSING",
            completed["service_failures"]["epos"]["code"],
        )
        self.assertEqual(
            "PARTIAL_ACQUISITION_FAILED",
            completed["error"]["code"],
        )
        self.assertNotIn("should-not-persist", repr(completed))

        with closing(sqlite3.connect(self.store.path)) as connection:
            durable_text = "\n".join(
                str(value)
                for table, column in (
                    ("batch_jobs", "error_json"),
                    ("batch_jobs", "result_json"),
                    ("job_events", "payload_json"),
                )
                for row in connection.execute(f"SELECT {column} FROM {table}")
                for value in row
            )
        self.assertNotIn("should-not-persist", durable_text)

    def test_service_specific_login_failure_does_not_block_other_services(self) -> None:
        calls: list[str] = []

        def blocked_runner(**kwargs):
            service_id = kwargs["service_id"]
            calls.append(service_id)
            if service_id != "epos":
                return SimpleNamespace(
                    success=True,
                    action_required=False,
                    failure=None,
                )
            return SimpleNamespace(
                success=False,
                action_required=False,
                failure=SimpleNamespace(
                    code="LOGIN_REJECTED",
                    message="wrong password secret=should-not-persist",
                ),
            )

        self.worker.acquisition_runner = blocked_runner
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos", "commufa"],
            idempotency_key="blocking-auth-failure",
        )

        self.assertTrue(self.worker.run_once())
        stopped = self.service.get_job(created["id"], owner_id="owner-1")

        self.assertEqual(["epos", "commufa"], calls)
        self.assertEqual("failed", stopped["state"])
        self.assertEqual(["commufa"], stopped["completed_service_ids"])
        self.assertEqual(["epos"], stopped["failed_service_ids"])
        self.assertEqual(
            "LOGIN_REJECTED",
            stopped["service_failures"]["epos"]["code"],
        )
        self.assertEqual("PARTIAL_ACQUISITION_FAILED", stopped["error"]["code"])
        self.assertNotIn("should-not-persist", repr(stopped))

    def test_explicit_account_lock_stops_before_next_service(self) -> None:
        calls: list[str] = []

        def locked_runner(**kwargs):
            calls.append(kwargs["service_id"])
            return SimpleNamespace(
                success=False,
                action_required=False,
                failure=SimpleNamespace(
                    code="ACCOUNT_LOCKED",
                    message="provider locked this account",
                ),
            )

        self.worker.acquisition_runner = locked_runner
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos", "commufa"],
            idempotency_key="blocking-account-lock",
        )

        self.assertTrue(self.worker.run_once())
        stopped = self.service.get_job(created["id"], owner_id="owner-1")

        self.assertEqual(["epos"], calls)
        self.assertEqual("failed", stopped["state"])
        self.assertEqual([], stopped["completed_service_ids"])
        self.assertEqual([], stopped["failed_service_ids"])
        self.assertEqual("ACCOUNT_LOCKED", stopped["error"]["code"])

    def test_security_failure_does_not_persist_provider_message(self) -> None:
        def security_runner(**_kwargs):
            return SimpleNamespace(
                success=False,
                action_required=False,
                failure=SimpleNamespace(
                    code="SECURITY_CHALLENGE",
                    message="provider secret=must-not-persist",
                ),
            )

        self.worker.acquisition_runner = security_runner
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["epos"],
            idempotency_key="security-message-sanitized",
        )

        self.assertTrue(self.worker.run_once())
        stopped = self.service.get_job(created["id"], owner_id="owner-1")

        self.assertEqual("intervention_required", stopped["state"])
        self.assertEqual("SECURITY_CHALLENGE", stopped["error"]["code"])
        self.assertNotIn("must-not-persist", repr(stopped))
        with closing(sqlite3.connect(self.store.path)) as connection:
            durable = "\n".join(
                str(value)
                for table, column in (
                    ("batch_jobs", "error_json"),
                    ("job_events", "payload_json"),
                )
                for row in connection.execute(f"SELECT {column} FROM {table}")
                for value in row
            )
        self.assertNotIn("must-not-persist", durable)

    def test_cancellation_callback_reads_durable_job_state(self) -> None:
        calls: list[str] = []
        created: dict[str, Any] = {}

        def cancellation_aware_runner(**kwargs):
            calls.append(kwargs["service_id"])
            callback = kwargs.get("cancellation_requested")
            self.assertTrue(callable(callback))
            self.assertFalse(callback())
            self.service.cancel_job(created["id"], owner_id="owner-1")
            self.assertTrue(callback())
            return SimpleNamespace(
                success=False,
                action_required=False,
                failure=SimpleNamespace(
                    code="ACQUISITION_CANCELLED",
                    message="cancelled",
                ),
            )

        self.worker.acquisition_runner = cancellation_aware_runner
        created.update(
            self.service.create_job(
                owner_id="owner-1",
                target_month="2026-07",
                service_ids=["epos", "commufa"],
                idempotency_key="durable-cancellation-check",
            )
        )

        self.assertTrue(self.worker.run_once())
        cancelled = self.service.get_job(created["id"], owner_id="owner-1")

        self.assertEqual(["epos"], calls)
        self.assertEqual("cancelled", cancelled["state"])
        self.assertEqual([], cancelled["completed_service_ids"])
        self.assertEqual([], cancelled["failed_service_ids"])

    def test_cancel_retries_cas_before_discarding_pending_challenge(self) -> None:
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["commufa"],
            idempotency_key="cancel-cas-race",
        )
        running = self.store.claim_next(owner="owner-1")
        assert running is not None
        challenge = Challenge.new(
            job_id=running.id,
            type=ChallengeType.OTP_EMAIL,
            message="確認コードを入力してください。",
        )
        self.store.add_challenge(challenge, owner="owner-1")
        self.inbox.register(
            challenge_id=str(challenge.id),
            owner_id="owner-1",
            ttl_seconds=5,
        )
        self.store.compare_and_set(
            running.id,
            owner="owner-1",
            expected_version=running.version,
            state=BatchJobState.WAITING_FOR_CHALLENGE,
            current="commufa",
        )

        original_compare_and_set = self.store.compare_and_set
        original_discard = self.inbox.discard
        call_order: list[str] = []
        first_cancel_attempt = True

        def racing_compare_and_set(*args, **kwargs):
            nonlocal first_cancel_attempt
            if kwargs.get("event_type") == "job_cancelled":
                call_order.append("cancel_cas")
                if first_cancel_attempt:
                    first_cancel_attempt = False
                    latest = self.store.get_job(created["id"], owner="owner-1")
                    original_compare_and_set(
                        latest.id,
                        owner="owner-1",
                        expected_version=latest.version,
                        state=BatchJobState.WAITING_FOR_CHALLENGE,
                        current="commufa",
                        event_type="simulated_worker_race",
                    )
                    raise VersionConflictError("simulated stale cancellation")
            return original_compare_and_set(*args, **kwargs)

        def recording_discard(challenge_id: str) -> bool:
            call_order.append("discard")
            return original_discard(challenge_id)

        with (
            patch.object(
                self.store,
                "compare_and_set",
                side_effect=racing_compare_and_set,
            ),
            patch.object(self.inbox, "discard", side_effect=recording_discard),
        ):
            cancelled = self.service.cancel_job(
                created["id"],
                owner_id="owner-1",
            )

        self.assertEqual("cancelled", cancelled["state"])
        self.assertEqual(["cancel_cas", "cancel_cas", "discard"], call_order)
        self.assertFalse(self.inbox.is_pending(str(challenge.id)))

    def test_cancel_winning_after_final_check_prevents_drive_upload(self) -> None:
        final_check_seen = threading.Event()
        release_final_check = threading.Event()

        class RaceStorage:
            def __init__(self) -> None:
                self.upserts: list[dict[str, Any]] = []

            def list_files(self) -> list[dict[str, str]]:
                return []

            def upsert_bytes(self, **kwargs):
                self.upserts.append(dict(kwargs))
                return {"id": "unexpected-upload"}

        class PdfFetcher:
            @staticmethod
            def fetch_pdf(_target_month: str):
                return SimpleNamespace(
                    content=b"%PDF-1.7\nGetReceipt cancellation race",
                    metadata_text="2026年7月18日 ご請求額 8,250円",
                )

        storage = RaceStorage()

        def racing_acquisition_runner(**kwargs):
            durable_check = kwargs["cancellation_requested"]
            checks = 0

            def pause_after_final_check() -> bool:
                nonlocal checks
                checks += 1
                requested = bool(durable_check())
                if checks == 4 and not requested:
                    final_check_seen.set()
                    self.assertTrue(release_final_check.wait(timeout=5))
                return requested

            kwargs["cancellation_requested"] = pause_after_final_check
            return run_auto_acquisition(**kwargs)

        self.worker.storage_factory = lambda: storage
        self.worker.fetcher_factory = lambda *_args: PdfFetcher()
        self.worker.acquisition_runner = racing_acquisition_runner
        created = self.service.create_job(
            owner_id="owner-1",
            target_month="2026-07",
            service_ids=["commufa"],
            idempotency_key="cancel-before-drive-commit",
        )

        worker_thread = threading.Thread(target=self.worker.run_once)
        worker_thread.start()
        self.assertTrue(final_check_seen.wait(timeout=5))

        cancelled: dict[str, Any] = {}

        def cancel() -> None:
            cancelled.update(
                self.service.cancel_job(created["id"], owner_id="owner-1")
            )

        cancel_thread = threading.Thread(target=cancel)
        cancel_thread.start()
        cancel_thread.join(timeout=5)
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual("cancelled", cancelled["state"])

        release_final_check.set()
        worker_thread.join(timeout=5)
        self.assertFalse(worker_thread.is_alive())
        self.assertEqual([], storage.upserts)
        self.assertEqual(
            "cancelled",
            self.service.get_job(created["id"], owner_id="owner-1")["state"],
        )

    def test_microsoft_reconnect_errors_are_public_and_actionable(self) -> None:
        for code in (
            "MICROSOFT_OAUTH_RECONNECT_REQUIRED",
            "MICROSOFT_TOKEN_DECRYPT_FAILED",
        ):
            with self.subTest(code=code):
                internal = RuntimeError("sensitive token-store detail")
                internal.code = code  # type: ignore[attr-defined]

                public = self.service._microsoft_oauth_error(internal)

                self.assertEqual(code, public.code)
                self.assertEqual(409, public.status_code)
                self.assertEqual("Microsoftメールを再接続してください。", str(public))
                self.assertNotIn("sensitive", str(public))

    def _wait_for_state(self, job_id: str, expected: str) -> dict:
        deadline = time.time() + 5
        latest = {}
        while time.time() < deadline:
            latest = self.service.get_job(job_id, owner_id="owner-1")
            if latest["state"] == expected:
                return latest
            time.sleep(0.01)
        self.fail(f"job did not reach {expected}: {latest}")


if __name__ == "__main__":
    unittest.main()
