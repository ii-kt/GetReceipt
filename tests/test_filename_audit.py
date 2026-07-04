from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.domain.ledger import ReceiptLedger, rows_to_csv_bytes  # noqa: E402
from src.domain.naming import ReceiptMetadata, build_receipt_filename  # noqa: E402
from src.workflows.filename_audit import (  # noqa: E402
    audit_display_rows,
    audit_summary,
    build_drive_filename_audit_rows,
    build_rename_metadata,
    refresh_drive_filename_audit,
    rename_synced_ledger_file,
)


class FakeStorage:
    def __init__(self, files: list[dict[str, str]], ledger_rows: list[dict[str, str]] | None = None) -> None:
        self.files = files
        self.ledger_content = rows_to_csv_bytes(ledger_rows or [])
        self.upserts: list[dict[str, Any]] = []

    def list_files(self) -> list[dict[str, str]]:
        return self.files

    def download_bytes_by_name(self, file_name: str) -> bytes | None:
        if file_name == "_receipt_index.csv":
            return self.ledger_content
        return None

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> None:
        self.upserts.append({"file_name": file_name, "content": content, "mime_type": mime_type})
        if file_name == "_receipt_index.csv":
            self.ledger_content = content


class FilenameAuditTest(unittest.TestCase):
    def test_audit_marks_expected_filename_as_ok(self) -> None:
        metadata = ReceiptMetadata(
            service_id="mobile",
            service_label="携帯",
            target_month="2026-07",
            transaction_date=date(2026, 7, 1),
            partner_name="株式会社NTTドコモ",
            amount_yen=8250,
        )
        file_name = build_receipt_filename(metadata, "pdf")
        rows = build_drive_filename_audit_rows(
            [{"id": "drive-1", "name": file_name, "mimeType": "application/pdf"}],
            [{"drive_file_id": "drive-1", **metadata.to_record()}],
        )

        self.assertEqual(rows[0]["判定"], "OK")
        self.assertEqual(audit_summary(rows), {"total": 1, "ok": 1, "review": 0, "managed": 0})
        self.assertNotIn("ファイルID", audit_display_rows(rows)[0])

    def test_refresh_repairs_missing_drive_id_from_matching_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ReceiptLedger(Path(temp_dir) / "receipt_index.csv")
            metadata = ReceiptMetadata(
                service_id="commufa",
                service_label="Wi-Fi",
                target_month="2026-07",
                transaction_date=date(2026, 7, 2),
                partner_name="中部テレコミュニケーション株式会社",
                amount_yen=5500,
            )
            file_name = build_receipt_filename(metadata, "pdf")
            ledger.append_upload(
                metadata=metadata,
                file_name=file_name,
                drive_file_id="",
                drive_web_view_link="",
                sha256="hash",
            )
            storage = FakeStorage(
                [{"id": "drive-2", "name": file_name, "mimeType": "application/pdf", "webViewLink": "https://drive"}]
            )

            rows = refresh_drive_filename_audit(storage, ledger)

            self.assertEqual(rows[0]["判定"], "OK")
            self.assertEqual(ledger.read()[0]["drive_file_id"], "drive-2")
            self.assertEqual(storage.upserts[0]["file_name"], "_receipt_index.csv")

    def test_rename_synced_ledger_file_adds_unregistered_drive_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ReceiptLedger(Path(temp_dir) / "receipt_index.csv")
            storage = FakeStorage([])
            row = {
                "サービスID": "mobile",
                "サービス名": "携帯",
                "対象月": "2026-07",
                "通貨": "JPY",
                "取得元URL": "https://example.test",
                "元ファイル名": "download.pdf",
            }
            metadata = build_rename_metadata(
                row=row,
                transaction_date=date(2026, 7, 1),
                partner_name="株式会社NTTドコモ",
                amount_yen=8250,
            )

            rename_synced_ledger_file(
                storage,
                ledger,
                drive_file_id="drive-3",
                file_name="20260701_株式会社NTTドコモ_8250円.pdf",
                metadata=metadata,
                drive_web_view_link="https://drive",
                original_file_name="download.pdf",
            )

            saved = ledger.read()[0]
            self.assertEqual(saved["drive_file_id"], "drive-3")
            self.assertEqual(saved["status"], "uploaded")
            self.assertEqual(saved["partner_name"], "株式会社NTTドコモ")


if __name__ == "__main__":
    unittest.main()
