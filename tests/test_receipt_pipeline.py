from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.ledger import ReceiptLedger  # noqa: E402
from src.receipt_pipeline import upload_auto_receipt_to_drive  # noqa: E402


@dataclass(frozen=True)
class FakeDriveResult:
    id: str
    name: str
    web_view_link: str


class FakeStorage:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.upserts: list[dict[str, object]] = []

    def upload_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> FakeDriveResult:
        self.uploads.append({"file_name": file_name, "content": content, "mime_type": mime_type})
        return FakeDriveResult("drive-file-id", file_name, "https://drive.example/file")

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> FakeDriveResult:
        self.upserts.append({"file_name": file_name, "content": content, "mime_type": mime_type})
        return FakeDriveResult("ledger-id", file_name, "https://drive.example/ledger")


class ReceiptPipelineTest(unittest.TestCase):
    def test_upload_auto_receipt_to_drive_records_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = FakeStorage()
            ledger = ReceiptLedger(Path(temp_dir) / "receipt_index.csv")
            saved = upload_auto_receipt_to_drive(
                service_id="epos",
                target_month="2026-01",
                content=b"%PDF-1.4",
                original_file_name="epos_2026-01.pdf",
                source_url="https://www.eposcard.co.jp/",
                metadata_text="お支払日 2026年1月27日 ご請求金額 90,000円",
                storage=storage,  # type: ignore[arg-type]
                ledger=ledger,
            )

        self.assertEqual(saved["status"], "uploaded")
        self.assertEqual(saved["target_month"], "2026-01")
        self.assertEqual(saved["amount_yen"], "90000")
        self.assertEqual(storage.uploads[0]["file_name"], "20260127_株式会社エポスカード_90000円.pdf")
        self.assertEqual(storage.upserts[0]["file_name"], "_receipt_index.csv")


if __name__ == "__main__":
    unittest.main()
