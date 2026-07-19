from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.workflows.manual_upload import (  # noqa: E402
    ManualUploadError,
    inspect_manual_receipt,
    save_manual_receipt,
)
from src.domain.acquisition import AcquisitionOutcome  # noqa: E402


class FakeStorage:
    def __init__(self) -> None:
        self.files: list[dict[str, str]] = []
        self.lock = threading.Lock()

    def list_files(self):
        with self.lock:
            return list(self.files)

    def upsert_bytes(self, *, file_name, content, mime_type):
        self.assertions = (content.startswith(b"%PDF"), mime_type)
        file = {
            "id": file_name,
            "name": file_name,
            "mimeType": mime_type,
            "size": str(len(content)),
            "webViewLink": f"https://drive.google.com/{file_name}",
        }
        with self.lock:
            self.files.append(file)
        return file


class ManualUploadTest(unittest.TestCase):
    def test_iphone_pdf_uses_standard_drive_pipeline(self) -> None:
        content = (
            "%PDF-1.7\n"
            "フラットエナジー株式会社 トクテンでんき 2026年8月 "
            "ご請求金額 12,345円\n%%EOF"
        ).encode()
        inspection = inspect_manual_receipt(
            service_id="tokuten",
            target_month="2026-07",
            content=content,
        )
        self.assertEqual(12345, inspection.amount_yen)
        self.assertFalse(inspection.requires_confirmation)

        storage = FakeStorage()
        result = save_manual_receipt(
            service_id="tokuten",
            target_month="2026-07",
            content=content,
            original_file_name="iphone.pdf",
            storage=storage,
            confirmed=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(1, len(storage.files))
        self.assertIn("フラットエナジー株式会社", storage.files[0]["name"])

    def test_unverified_partner_and_month_require_explicit_confirmation(self) -> None:
        content = b"%PDF-1.7\nInvoice total 9,999 yen 9,999\x89~\n%%EOF"
        inspection = inspect_manual_receipt(
            service_id="epos",
            target_month="2026-07",
            content=content,
        )
        self.assertTrue(inspection.requires_confirmation)
        with self.assertRaises(ManualUploadError):
            save_manual_receipt(
                service_id="epos",
                target_month="2026-07",
                content=content,
                original_file_name="unknown.pdf",
                storage=FakeStorage(),
                confirmed=False,
            )

    def test_non_pdf_is_rejected(self) -> None:
        with self.assertRaises(ManualUploadError):
            inspect_manual_receipt(
                service_id="mobile",
                target_month="2026-07",
                content=b"not a pdf",
            )

    def test_concurrent_manual_retries_create_only_one_drive_receipt(self) -> None:
        content = (
            "%PDF-1.7\n"
            "フラットエナジー株式会社 トクデンき 2026年8月 "
            "ご請求金額 12,345円\n%%EOF"
        ).encode()
        storage = FakeStorage()
        barrier = threading.Barrier(2)
        results = []
        failures: list[Exception] = []

        def save() -> None:
            try:
                barrier.wait(timeout=2)
                results.append(
                    save_manual_receipt(
                        service_id="tokuten",
                        target_month="2026-07",
                        content=content,
                        original_file_name="iphone.pdf",
                        storage=storage,
                        confirmed=False,
                    )
                )
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual([], failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, len(storage.files))
        self.assertEqual(
            {AcquisitionOutcome.ACQUIRED, AcquisitionOutcome.ALREADY_EXISTS},
            {result.outcome for result in results},
        )


if __name__ == "__main__":
    unittest.main()
