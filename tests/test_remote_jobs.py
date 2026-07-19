from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.jobs.client import WorkerApiError  # noqa: E402
from src.ui.remote_jobs import (  # noqa: E402
    idempotency_key,
    job_id_from_query_params,
    job_state,
    recover_remote_job,
    service_progress_job,
    validate_challenge_response,
)


class FakeClient:
    def __init__(self, *, jobs=None, active=None) -> None:
        self.jobs = dict(jobs or {})
        self.active = active
        self.requested_ids: list[str] = []
        self.active_months: list[str] = []

    def get_job(self, job_id: str):
        self.requested_ids.append(job_id)
        if job_id not in self.jobs:
            raise WorkerApiError(
                "ジョブが見つかりません。",
                code="JOB_NOT_FOUND",
                status_code=404,
            )
        return self.jobs[job_id]

    def find_active_job(self, target_month: str):
        self.active_months.append(target_month)
        return self.active


class RemoteJobsTest(unittest.TestCase):
    def test_query_job_id_is_strictly_validated(self) -> None:
        self.assertEqual(job_id_from_query_params({"job": "job_123-ABC"}), "job_123-ABC")
        self.assertEqual(job_id_from_query_params({"job": ["job-1"]}), "job-1")
        self.assertEqual(job_id_from_query_params({"job": "../secret"}), "")
        self.assertEqual(job_id_from_query_params({"job": "job-1?token=x"}), "")

    def test_idempotency_key_is_stable_and_retry_specific(self) -> None:
        first = idempotency_key(
            target_month="2026-07",
            service_ids=["epos", "commufa"],
        )
        duplicate = idempotency_key(
            target_month="2026-07",
            service_ids=["epos", "commufa"],
        )
        retry = idempotency_key(
            target_month="2026-07",
            service_ids=["epos", "commufa"],
            previous_job_id="job-1",
        )
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, retry)
        self.assertRegex(first, r"^mobile-[0-9a-f]{64}$")

    def test_stale_job_query_falls_back_to_active_job(self) -> None:
        active = {
            "id": "job-active",
            "target_month": "2026-07",
            "state": "running",
        }
        client = FakeClient(active=active)

        view = recover_remote_job(
            client,
            target_month="2026-07",
            query_params={"job": "job-deleted"},
        )

        self.assertEqual(active, view.job)
        self.assertIsNone(view.api_error)
        self.assertEqual(["job-deleted"], client.requested_ids)
        self.assertEqual(["2026-07"], client.active_months)

    def test_active_job_takes_precedence_over_old_terminal_query(self) -> None:
        terminal = {
            "id": "job-old",
            "target_month": "2026-07",
            "state": "succeeded",
        }
        active = {
            "id": "job-current",
            "target_month": "2026-07",
            "state": "waiting_for_challenge",
        }
        client = FakeClient(jobs={"job-old": terminal}, active=active)

        view = recover_remote_job(
            client,
            target_month="2026-07",
            query_params={"job": "job-old"},
        )

        self.assertEqual("job-current", view.job["id"])

    def test_terminal_query_remains_visible_when_no_active_job_exists(self) -> None:
        terminal = {
            "id": "job-old",
            "target_month": "2026-07",
            "state": "failed",
        }
        client = FakeClient(jobs={"job-old": terminal})

        view = recover_remote_job(
            client,
            target_month="2026-07",
            query_params={"job": "job-old"},
        )

        self.assertEqual(terminal, view.job)

    def test_challenge_schema_is_not_hardcoded_to_six_digits(self) -> None:
        epos = {
            "input_schema": {
                "min_length": 3,
                "max_length": 3,
                "pattern": r"^[0-9]{3}$",
            }
        }
        commufa = {
            "input_schema": {
                "min_length": 6,
                "max_length": 6,
                "pattern": r"^[0-9]{6}$",
            }
        }
        webbilling = {
            "input_schema": {
                "min_length": 4,
                "max_length": 8,
                "pattern": r"^[0-9]{4,8}$",
            }
        }
        self.assertEqual(validate_challenge_response(epos, "１２３"), "123")
        self.assertEqual(validate_challenge_response(commufa, "654321"), "654321")
        self.assertEqual(validate_challenge_response(webbilling, "12345678"), "12345678")
        with self.assertRaises(ValueError):
            validate_challenge_response(epos, "123456")
        with self.assertRaises(ValueError):
            validate_challenge_response(commufa, "ABCDEF")

    def test_progress_adapter_does_not_include_challenge_response(self) -> None:
        job = {
            "state": "waiting_for_challenge",
            "service_ids": ["epos", "commufa"],
            "completed_service_ids": ["epos"],
            "current_service_id": "commufa",
            "challenge": {"id": "challenge-1"},
        }
        self.assertEqual(job_state(job), "waiting_for_challenge")
        self.assertEqual(
            service_progress_job(job),
            {
                "service_ids": ["epos", "commufa"],
                "completed": ["epos"],
                "failed_service_ids": [],
                "failed": {},
                "current_service": "commufa",
            },
        )

    def test_progress_adapter_exposes_only_sanitized_service_failures(self) -> None:
        progress = service_progress_job(
            {
                "service_ids": ["epos", "commufa"],
                "completed_service_ids": ["commufa"],
                "failed_service_ids": ["epos", "../invalid"],
                "service_failures": {
                    "epos": {
                        "code": "PDF_SIGNATURE_MISSING",
                        "message": "provider response with a secret",
                    }
                },
                "current_service_id": None,
            }
        )
        self.assertEqual(["epos"], progress["failed_service_ids"])
        self.assertEqual(
            "PDF_SIGNATURE_MISSING",
            progress["failed"]["epos"]["code"],
        )
        self.assertNotIn("secret", progress["failed"]["epos"]["message"])


if __name__ == "__main__":
    unittest.main()
