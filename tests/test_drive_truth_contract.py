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


def drive_file(
    name: str,
    *,
    mime_type: str = "application/pdf",
    size: int | str = "1024",
) -> dict[str, object]:
    return {
        "id": f"drive:{name}",
        "name": name,
        "mimeType": mime_type,
        "size": size,
        "webViewLink": f"https://drive.example/{name}",
    }


class DriveTruthContractTest(unittest.TestCase):
    def assert_stored(
        self,
        *,
        service_id: str,
        file: dict[str, object],
        target_month: str = "2026-07",
    ) -> None:
        files = [file]
        service = service_by_id(service_id)

        self.assertIsNotNone(find_receipt(files, service, target_month))
        self.assertEqual(
            receipt_month_state(files, service, target_month),
            ReceiptMonthState.STORED,
        )

    def assert_missing(
        self,
        *,
        service_id: str,
        file: dict[str, object],
        target_month: str = "2026-07",
    ) -> None:
        files = [file]
        service = service_by_id(service_id)

        self.assertIsNone(find_receipt(files, service, target_month))
        self.assertEqual(
            receipt_month_state(files, service, target_month),
            ReceiptMonthState.MISSING,
        )

    def test_canonical_drive_file_name_is_the_saved_receipt_contract(self) -> None:
        canonical_names = {
            "epos": "20260701_株式会社エポスカード_8250円.pdf",
            "commufa": "20260702_中部テレコミュニケーション株式会社_5720円.pdf",
            "tokuten": "20260703_フラットエナジー株式会社_4100円.pdf",
            "mobile": "20260704_NTTファイナンス株式会社_5500円.pdf",
        }

        for service_id, file_name in canonical_names.items():
            with self.subTest(service_id=service_id, file_name=file_name):
                self.assert_stored(
                    service_id=service_id,
                    file=drive_file(file_name),
                )

    def test_mobile_accepts_legacy_ntt_docomo_issuer_name(self) -> None:
        self.assert_stored(
            service_id="mobile",
            file=drive_file("20260715_株式会社NTTドコモ_5500円.pdf"),
        )

    def test_real_drive_filename_samples_match_all_four_recurring_services(self) -> None:
        samples = {
            "epos": "20260605_株式会社エポスカード_87560円.pdf",
            "commufa": "20260611_中部テレコミュニケーション株式会社_6710.pdf",
            "tokuten": "20260621_フラットエナジー株式会社_7515円.pdf",
            "mobile": "20260609_NTTファイナンス株式会社_4882円.pdf",
        }
        for service_id, file_name in samples.items():
            with self.subTest(service_id=service_id):
                self.assert_stored(
                    service_id=service_id,
                    file=drive_file(file_name),
                    target_month="2026-06",
                )

    def test_yenless_amount_is_only_accepted_for_commufa_legacy_files(self) -> None:
        self.assert_stored(
            service_id="commufa",
            file=drive_file("20260711_中部テレコミュニケーション株式会社_6710.pdf"),
        )
        self.assert_missing(
            service_id="epos",
            file=drive_file("20260705_株式会社エポスカード_87560.pdf"),
        )

    def test_octet_stream_is_accepted_when_pdf_extension_and_name_are_valid(self) -> None:
        self.assert_stored(
            service_id="epos",
            file=drive_file(
                "20260701_株式会社エポスカード_8250円.pdf",
                mime_type="application/octet-stream",
            ),
        )

    def test_zero_byte_drive_file_is_not_saved_receipt_evidence(self) -> None:
        for zero_size in (0, "0"):
            with self.subTest(size=zero_size):
                self.assert_missing(
                    service_id="epos",
                    file=drive_file(
                        "20260701_株式会社エポスカード_8250円.pdf",
                        size=zero_size,
                    ),
                )

    def test_non_amount_suffix_is_not_saved_receipt_evidence(self) -> None:
        invalid_names = (
            "20260701_株式会社エポスカード_memo.pdf",
            "20260701_株式会社エポスカード_8250円_memo.pdf",
            "20260701_株式会社エポスカード_8250.pdf",
            "20260701_株式会社エポスカード_円.pdf",
            "20260701_株式会社エポスカード_請求書_8250円.pdf",
            "20260701_株式会社エポスカード_8,250円.pdf",
            "20260701_株式会社エポスカード_,1,円.pdf",
        )

        for file_name in invalid_names:
            with self.subTest(file_name=file_name):
                self.assert_missing(
                    service_id="epos",
                    file=drive_file(file_name),
                )

    def test_missing_size_is_not_saved_receipt_evidence(self) -> None:
        file = drive_file("20260701_株式会社エポスカード_8250円.pdf")
        file.pop("size")
        self.assert_missing(service_id="epos", file=file)

    def test_selectable_months_follow_tokyo_local_month_boundary(self) -> None:
        # ``today`` is the application-facing Tokyo local date.  The selectable
        # range must advance exactly when that local calendar enters a new month.
        before_boundary = selectable_months(date(2026, 7, 31))
        after_boundary = selectable_months(date(2026, 8, 1))

        self.assertEqual(before_boundary[-1], "2026-07")
        self.assertNotIn("2026-08", before_boundary)
        self.assertEqual(after_boundary[-1], "2026-08")
        self.assertEqual(after_boundary[:-1], before_boundary)


if __name__ == "__main__":
    unittest.main()
