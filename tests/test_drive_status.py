from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.config import selectable_months, service_by_id  # noqa: E402
from src.workflows.drive_status import (  # noqa: E402
    ReceiptMonthState,
    find_receipt,
    receipt_month_state,
)


class DriveStatusTest(unittest.TestCase):
    def test_find_receipt_matches_month_partner_and_pdf(self) -> None:
        files = [
            {
                "id": "drive-1",
                "name": "20260701_株式会社エポスカード_8250円.pdf",
                "mimeType": "application/pdf",
                "size": "1234",
                "modifiedTime": "2026-07-02T03:04:05Z",
                "webViewLink": "https://drive.example/drive-1",
            }
        ]

        receipt = find_receipt(files, service_by_id("epos"), "2026-07")

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.file_id, "drive-1")
        self.assertEqual(receipt.size, 1234)
        self.assertEqual(receipt_month_state(files, service_by_id("epos"), "2026-07"), ReceiptMonthState.STORED)

    def test_find_receipt_normalizes_full_width_and_spacing(self) -> None:
        files = [
            {
                "id": "drive-2",
                "name": "２０２６０７０１_株式会社 エポスカード_８２５０円.ＰＤＦ",
                "mimeType": "application/pdf",
                "size": "1024",
            }
        ]

        receipt = find_receipt(files, service_by_id("epos"), "2026-07")

        self.assertIsNotNone(receipt)

    def test_mobile_accepts_both_partner_aliases(self) -> None:
        service = service_by_id("mobile")
        aliases = (
            "NTTファイナンス株式会社",
            "株式会社NTTファイナンス",
        )

        for index, partner in enumerate(aliases):
            with self.subTest(partner=partner):
                files = [
                    {
                        "id": f"mobile-{index}",
                        "name": f"20260715_{partner}_5500円.pdf",
                        "mimeType": "application/pdf",
                        "size": "1024",
                    }
                ]
                self.assertIsNotNone(find_receipt(files, service, "2026-07"))

    def test_find_receipt_rejects_wrong_month_partner_or_filename(self) -> None:
        files = [
            {"id": "wrong-month", "name": "20260601_株式会社エポスカード_8250円.pdf", "mimeType": "application/pdf"},
            {"id": "wrong-partner", "name": "20260701_別会社_8250円.pdf", "mimeType": "application/pdf"},
            {"id": "invalid-date", "name": "20260732_株式会社エポスカード_8250円.pdf", "mimeType": "application/pdf"},
            {"id": "wrong-extension", "name": "20260701_株式会社エポスカード_8250円.csv", "mimeType": "application/pdf"},
            {"id": "wrong-format", "name": "202607_株式会社エポスカード_8250円.pdf", "mimeType": "application/pdf"},
        ]

        self.assertIsNone(find_receipt(files, service_by_id("epos"), "2026-07"))
        self.assertEqual(
            receipt_month_state(files, service_by_id("epos"), "2026-07"),
            ReceiptMonthState.MISSING,
        )

    def test_selectable_months_stops_at_current_month(self) -> None:
        months = selectable_months(date(2026, 7, 13))

        self.assertEqual(months[0], "2026-01")
        self.assertEqual(months[-1], "2026-07")
        self.assertNotIn("2026-08", months)
        self.assertEqual(service_by_id("mobile").default_partner, "NTTファイナンス株式会社")

    def test_service_partner_aliases_match_drive_issuer_names(self) -> None:
        self.assertEqual(service_by_id("epos").partner_aliases, ("株式会社エポスカード",))
        self.assertEqual(service_by_id("commufa").partner_aliases, ("中部テレコミュニケーション株式会社",))
        self.assertEqual(service_by_id("tokuten").partner_aliases, ("フラットエナジー株式会社",))
        self.assertEqual(
            service_by_id("mobile").partner_aliases,
            ("NTTファイナンス株式会社", "株式会社NTTファイナンス", "株式会社NTTドコモ"),
        )


if __name__ == "__main__":
    unittest.main()
