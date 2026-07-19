from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.jobs import (  # noqa: E402
    BatchJob,
    BatchJobState,
    Challenge,
    ChallengeInputSchema,
    ChallengeInputType,
    ChallengeType,
    IdempotencyConflictError,
    JobNotFoundError,
    SQLiteJobStore,
    VersionConflictError,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class JobModelTest(unittest.TestCase):
    def test_batch_job_dict_round_trip_preserves_public_types(self) -> None:
        created_at = datetime(2026, 7, 19, 1, 2, 3, tzinfo=timezone.utc)
        original = BatchJob.new(
            owner="user-subject",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
            now=created_at,
        )
        updated = replace(
            original,
            state=BatchJobState.RUNNING,
            current="epos",
            version=2,
            updated_at=created_at + timedelta(minutes=1),
            result={"drive_file_id": "drive-123"},
        )

        restored = BatchJob.from_dict(updated.to_dict())

        self.assertEqual(updated, restored)
        self.assertIsInstance(restored.id, UUID)
        self.assertEqual("running", restored.to_dict()["state"])
        self.assertEqual(["epos", "commufa"], restored.to_dict()["service_ids"])

    def test_challenge_dict_round_trip_includes_input_schema_and_public_metadata(self) -> None:
        created_at = datetime(2026, 7, 19, 1, 0, tzinfo=timezone.utc)
        challenge = Challenge.new(
            job_id=BatchJob.new(
                owner="owner",
                target_month="2026-07",
                service_ids=("commufa",),
                now=created_at,
            ).id,
            type=ChallengeType.OTP_EMAIL,
            message="メール確認コードを入力してください。",
            input_schema=ChallengeInputSchema(
                input_type=ChallengeInputType.CODE,
                label="確認コード",
                required=True,
                min_length=6,
                max_length=6,
                pattern=r"^[0-9]{6}$",
                autocomplete="one-time-code",
            ),
            metadata={
                "masked_destination": "k***@example.com",
                "attempts_remaining": 3,
            },
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=10),
        )

        self.assertEqual(challenge, Challenge.from_dict(challenge.to_dict()))

    def test_secret_fields_are_rejected_from_durable_metadata(self) -> None:
        job = BatchJob.new(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos",),
        )
        with self.assertRaisesRegex(ValueError, "forbidden secret field"):
            Challenge.new(
                job_id=job.id,
                type=ChallengeType.OTP_SMS,
                message="コードが必要です。",
                metadata={"otp_code": "123456"},
            )


class SQLiteJobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.clock = FixedClock()
        self.store = SQLiteJobStore(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_job_persists_across_store_instances(self) -> None:
        created = self.store.create_job(
            owner="owner-a",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
        )
        self.store.close()

        self.store = SQLiteJobStore(self.db_path, clock=self.clock)
        loaded = self.store.get_job(created.id, owner="owner-a")

        self.assertEqual(created, loaded)

    def test_create_is_idempotent_for_same_owner_key_and_request(self) -> None:
        first = self.store.create_job(
            owner="owner-a",
            target_month="2026-07",
            service_ids=("epos", "mobile"),
            idempotency_key="monthly-2026-07",
        )
        second = self.store.create_job(
            owner="owner-a",
            target_month="2026-07",
            service_ids=("epos", "mobile"),
            idempotency_key="monthly-2026-07",
        )

        self.assertEqual(first, second)
        self.assertEqual(1, len([event for event in self.store.list_events(first.id, owner="owner-a")]))

    def test_idempotency_key_reuse_with_different_request_is_rejected(self) -> None:
        self.store.create_job(
            owner="owner-a",
            target_month="2026-07",
            service_ids=("epos",),
            idempotency_key="request-key",
        )

        with self.assertRaises(IdempotencyConflictError):
            self.store.create_job(
                owner="owner-a",
                target_month="2026-08",
                service_ids=("epos",),
                idempotency_key="request-key",
            )

    def test_owner_mismatch_does_not_reveal_job_or_challenge(self) -> None:
        job = self.store.create_job(
            owner="owner-a",
            target_month="2026-07",
            service_ids=("commufa",),
        )
        challenge = Challenge.new(
            job_id=job.id,
            type=ChallengeType.PUSH_APPROVAL,
            message="スマートフォンで承認してください。",
        )
        self.store.add_challenge(challenge, owner="owner-a")

        with self.assertRaises(JobNotFoundError):
            self.store.get_job(job.id, owner="owner-b")
        with self.assertRaises(JobNotFoundError):
            self.store.get_challenge(challenge.id, owner="owner-b")

    def test_compare_and_set_increments_version_and_rejects_stale_writer(self) -> None:
        job = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
        )
        running = self.store.compare_and_set(
            job.id,
            owner="owner",
            expected_version=0,
            state=BatchJobState.RUNNING,
            current="epos",
            event_type="service_started",
            event_payload={"service_id": "epos"},
        )

        self.assertEqual(1, running.version)
        self.assertEqual(BatchJobState.RUNNING, running.state)
        self.assertEqual("epos", running.current)

        with self.assertRaises(VersionConflictError):
            self.store.compare_and_set(
                job.id,
                owner="owner",
                expected_version=0,
                state=BatchJobState.FAILED,
            )

    def test_claim_next_is_atomic_and_active_job_is_recoverable_by_month(self) -> None:
        first = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
            idempotency_key="first",
        )
        self.store.create_job(
            owner="owner",
            target_month="2026-08",
            service_ids=("mobile",),
            idempotency_key="second",
        )

        claimed = self.store.claim_next(owner="owner")

        self.assertEqual(first.id, claimed.id)
        self.assertEqual(BatchJobState.RUNNING, claimed.state)
        self.assertEqual("epos", claimed.current)
        self.assertEqual(
            claimed,
            self.store.find_active_job(owner="owner", target_month="2026-07"),
        )
        self.assertIsNone(
            self.store.find_active_job(owner="different-owner", target_month="2026-07")
        )

    def test_worker_restart_requeues_live_job_without_losing_completed_services(self) -> None:
        created = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
        )
        running = self.store.claim_next(owner="owner")
        waiting = self.store.compare_and_set(
            running.id,
            owner="owner",
            expected_version=running.version,
            state=BatchJobState.WAITING_FOR_CHALLENGE,
            completed=("epos",),
            current="commufa",
        )

        recovered_count = self.store.recover_incomplete_jobs(owner="owner")
        recovered = self.store.get_job(created.id, owner="owner")

        self.assertEqual(1, recovered_count)
        self.assertEqual(BatchJobState.QUEUED, recovered.state)
        self.assertEqual(("epos",), recovered.completed)
        self.assertIsNone(recovered.current)
        self.assertGreater(recovered.version, waiting.version)
        self.assertEqual(
            "worker_recovered_job",
            self.store.list_events(created.id, owner="owner")[-1].event_type,
        )

    def test_recovered_partial_job_does_not_retry_failed_service(self) -> None:
        created = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos", "commufa", "mobile"),
        )
        running = self.store.claim_next(owner="owner")
        self.store.compare_and_set(
            running.id,
            owner="owner",
            expected_version=running.version,
            state=BatchJobState.RUNNING,
            current="commufa",
            result={
                "completed_service_ids": [],
                "failed_service_ids": ["epos"],
                "service_failures": {
                    "epos": {
                        "code": "PDF_SIGNATURE_MISSING",
                        "message": "このサービスの自動取得を完了できませんでした。",
                        "retryable": True,
                    }
                },
            },
        )

        self.assertEqual(1, self.store.recover_incomplete_jobs(owner="owner"))
        reclaimed = self.store.claim_next(owner="owner")

        self.assertEqual(created.id, reclaimed.id)
        self.assertEqual("commufa", reclaimed.current)
        self.assertEqual(["epos"], reclaimed.result["failed_service_ids"])

    def test_challenge_metadata_is_persistent_but_has_no_response_column(self) -> None:
        job = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("commufa",),
        )
        created_at = datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc)
        challenge = Challenge.new(
            job_id=job.id,
            type=ChallengeType.OTP_EMAIL,
            message="確認コードを入力してください。",
            input_schema=ChallengeInputSchema(
                input_type=ChallengeInputType.CODE,
                required=True,
                min_length=6,
                max_length=6,
            ),
            metadata={"masked_destination": "k***@example.com"},
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=10),
        )
        self.store.add_challenge(challenge, owner="owner")
        self.store.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            challenge_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(challenges)")
            }
            serialized = connection.execute(
                "SELECT challenge_json FROM challenges WHERE id = ?",
                (str(challenge.id),),
            ).fetchone()[0]

        self.assertNotIn("response", challenge_columns)
        self.assertNotIn("otp", challenge_columns)
        self.assertNotIn("123456", serialized)

        self.store = SQLiteJobStore(self.db_path, clock=self.clock)
        self.assertEqual(challenge, self.store.get_challenge(challenge.id, owner="owner"))

    def test_event_payload_rejects_secret_fields_and_rolls_back(self) -> None:
        job = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos",),
        )
        original_events = self.store.list_events(job.id, owner="owner")

        for payload in (
            {"password": "do-not-store"},
            {"nested": {"access_token": "do-not-store"}},
            {"bytes": b"do-not-store"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.store.append_event(
                        job.id,
                        owner="owner",
                        event_type="unsafe",
                        payload=payload,
                    )

        self.assertEqual(original_events, self.store.list_events(job.id, owner="owner"))

    def test_completed_error_and_result_survive_cas_round_trip(self) -> None:
        job = self.store.create_job(
            owner="owner",
            target_month="2026-07",
            service_ids=("epos", "commufa"),
        )
        updated = self.store.compare_and_set(
            job.id,
            owner="owner",
            expected_version=0,
            state=BatchJobState.INTERVENTION_REQUIRED,
            completed=("epos",),
            current="commufa",
            error={"error_code": "CAPTCHA_PRESENT", "retryable": False},
            result={"drive_file_ids": ["drive-epos"]},
        )
        loaded = self.store.get_job(job.id, owner="owner")

        self.assertEqual(updated, loaded)
        self.assertEqual(("epos",), loaded.completed)
        self.assertEqual("CAPTCHA_PRESENT", loaded.error["error_code"])


if __name__ == "__main__":
    unittest.main()
