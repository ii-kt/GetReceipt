from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.domain.acquisition import AcquisitionOutcome, SecurityChallenge, Stage  # noqa: E402
from src.workflows.auto_acquisition import run_auto_acquisition  # noqa: E402


@dataclass(frozen=True)
class FakeStatement:
    content: bytes
    source_url: str = "https://example.test/statement"
    original_file_name: str = "statement.pdf"
    metadata_text: str = "2026年7月18日 ご請求額 8,250円"


class FakeFetchError(RuntimeError):
    code = "LOGIN_REQUIRED"


class FakeVerificationCodeRequired(RuntimeError):
    code = "SECURITY_CHALLENGE"
    challenge_kind = "verification_code"


class FakeCaptchaChallenge(RuntimeError):
    code = "SECURITY_CHALLENGE"
    challenge_kind = "captcha"


class FakeFetcher:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def fetch_pdf(self, target_month: str) -> FakeStatement:
        self.calls.append(target_month)
        if self.error is not None:
            raise self.error
        return FakeStatement(content=b"%PDF-1.7\nGetReceipt test PDF")


class FakeStorage:
    def __init__(self, files: list[dict[str, str]] | None = None, *, persist_on_upsert: bool = True) -> None:
        self.files = list(files or [])
        self.persist_on_upsert = persist_on_upsert
        self.list_calls = 0
        self.upserts: list[dict[str, Any]] = []

    def list_files(self) -> list[dict[str, str]]:
        self.list_calls += 1
        return [dict(file) for file in self.files]

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> dict[str, str]:
        self.upserts.append({"file_name": file_name, "content": content, "mime_type": mime_type})
        result = {
            "id": f"drive-{len(self.upserts)}",
            "name": file_name,
            "webViewLink": f"https://drive.test/{len(self.upserts)}",
            "mimeType": mime_type,
            "size": str(len(content)),
        }
        if self.persist_on_upsert:
            existing = next((file for file in self.files if file.get("name") == file_name), None)
            if existing is None:
                self.files.append(result)
            else:
                existing.update(result)
        return result


class AutoAcquisitionTest(unittest.TestCase):
    def test_success_fetches_saves_and_verifies_drive_file(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage()

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ACQUIRED)
        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(fetcher.calls, ["2026-07"])
        self.assertEqual(storage.list_calls, 3)
        self.assertEqual(len(storage.upserts), 1)
        self.assertEqual(storage.upserts[0]["mime_type"], "application/pdf")
        self.assertTrue(storage.upserts[0]["file_name"].startswith("20260801_中部テレコミュニケーション株式会社_"))
        self.assertEqual(result.file_name, storage.upserts[0]["file_name"])
        self.assertEqual(result.stage, Stage.COMPLETED)

    def test_existing_drive_file_skips_external_fetch_and_save(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage([
            {
                "id": "existing-id",
                "name": "20260801_中部テレコミュニケーション株式会社_8250円.pdf",
                "webViewLink": "https://drive.test/existing-id",
                "mimeType": "application/pdf",
                "size": "1024",
            }
        ])

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ALREADY_EXISTS)
        self.assertTrue(result.skipped)
        self.assertEqual(result.file_name, "20260801_中部テレコミュニケーション株式会社_8250円.pdf")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(storage.upserts, [])
        self.assertEqual(storage.list_calls, 1)

    def test_fetch_failure_returns_structured_terminal_result(self) -> None:
        fetcher = FakeFetcher(error=FakeFetchError("credentials rejected"))
        storage = FakeStorage()

        result = run_auto_acquisition(
            service_id="epos",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.outcome, AcquisitionOutcome.FAILED)
        self.assertEqual(result.error_code, "LOGIN_REQUIRED")
        self.assertEqual(result.stage, Stage.FAILED)
        self.assertEqual(result.failure.stage, Stage.FETCHING)
        self.assertNotIn("credentials rejected", result.failure.message)
        self.assertEqual(storage.upserts, [])

    def test_verification_code_challenge_is_non_terminal_action_required(self) -> None:
        fetcher = FakeFetcher(error=FakeVerificationCodeRequired("メールの認証コードを入力してください。"))
        storage = FakeStorage()

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ACTION_REQUIRED)
        self.assertFalse(result.success)
        self.assertTrue(result.action_required)
        self.assertEqual(result.stage, Stage.AWAITING_SECURITY_CODE)
        self.assertIsNone(result.failure)
        self.assertEqual(
            result.challenge,
            SecurityChallenge(
                kind="verification_code",
                message="メールの認証コードを入力してください。",
            ),
        )
        self.assertEqual(fetcher.calls, ["2026-07"])
        self.assertEqual(storage.list_calls, 1)
        self.assertEqual(storage.upserts, [])

    def test_captcha_becomes_interactive_action_required(self) -> None:
        fetcher = FakeFetcher(error=FakeCaptchaChallenge("CAPTCHAが表示されました。"))
        storage = FakeStorage()

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ACTION_REQUIRED)
        self.assertTrue(result.action_required)
        self.assertEqual(result.error_code, "")
        self.assertEqual(
            result.challenge,
            SecurityChallenge(
                kind="captcha",
                message="CAPTCHAが表示されました。",
            ),
        )
        self.assertIsNone(result.failure)
        self.assertEqual(result.stage, Stage.AWAITING_USER_ACTION)
        self.assertEqual(storage.upserts, [])

    def test_fetch_statement_override_reuses_the_standard_save_pipeline(self) -> None:
        fetcher = FakeFetcher(error=AssertionError("default fetcher must not be called"))
        storage = FakeStorage()
        resumed_months: list[str] = []

        def fetch_statement(target_month: str) -> FakeStatement:
            resumed_months.append(target_month)
            return FakeStatement(content=b"%PDF-1.7\nGetReceipt resumed PDF")

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
            fetch_statement=fetch_statement,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ACQUIRED)
        self.assertTrue(result.success)
        self.assertFalse(result.action_required)
        self.assertEqual(resumed_months, ["2026-07"])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(storage.list_calls, 3)
        self.assertEqual(len(storage.upserts), 1)

    def test_missing_file_after_save_fails_post_save_verification(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage(persist_on_upsert=False)

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SAVED_FILE_NOT_FOUND")
        self.assertEqual(result.failure.stage, Stage.VERIFYING)
        self.assertEqual(storage.list_calls, 3)
        self.assertEqual(len(storage.upserts), 1)

    def test_file_appearing_before_save_prevents_duplicate_upload(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage()
        existing = {
            "id": "concurrent-id",
            "name": "20260801_中部テレコミュニケーション株式会社_8250円.pdf",
            "webViewLink": "https://drive.test/concurrent-id",
            "mimeType": "application/pdf",
            "size": "1024",
        }
        original_list_files = storage.list_files

        def list_files_with_concurrent_save() -> list[dict[str, str]]:
            if storage.list_calls == 1:
                storage.files.append(existing)
            return original_list_files()

        storage.list_files = list_files_with_concurrent_save  # type: ignore[method-assign]

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(result.outcome, AcquisitionOutcome.ALREADY_EXISTS)
        self.assertEqual(fetcher.calls, ["2026-07"])
        self.assertEqual(storage.upserts, [])
        self.assertEqual(result.file_name, existing["name"])

    def test_second_run_is_idempotent_and_does_not_duplicate(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage()

        first = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )
        second = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
        )

        self.assertEqual(first.outcome, AcquisitionOutcome.ACQUIRED)
        self.assertEqual(second.outcome, AcquisitionOutcome.ALREADY_EXISTS)
        self.assertEqual(fetcher.calls, ["2026-07"])
        self.assertEqual(len(storage.upserts), 1)
        self.assertEqual(len(storage.files), 1)

    def test_cancellation_after_fetch_prevents_drive_save(self) -> None:
        fetcher = FakeFetcher()
        storage = FakeStorage()
        checks = 0

        def cancellation_requested() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        result = run_auto_acquisition(
            service_id="commufa",
            target_month="2026-07",
            fetcher=fetcher,
            storage=storage,
            cancellation_requested=cancellation_requested,
        )

        self.assertEqual(AcquisitionOutcome.FAILED, result.outcome)
        self.assertEqual("ACQUISITION_CANCELLED", result.error_code)
        self.assertEqual(["2026-07"], fetcher.calls)
        self.assertEqual([], storage.upserts)


if __name__ == "__main__":
    unittest.main()
