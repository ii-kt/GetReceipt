from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.config import (  # noqa: E402
    expected_transaction_month,
    service_by_id,
    usage_month_for_transaction,
)
from src.automation.epos import AcquisitionError, EposAutoFetcher  # noqa: E402
from src.automation.official_sites import build_tokuten_search_query  # noqa: E402
from src.workflows.drive_status import (  # noqa: E402
    ReceiptMonthState,
    find_receipt,
    receipt_month_state,
)


def drive_file(name: str) -> dict[str, object]:
    return {
        "id": f"drive:{name}",
        "name": name,
        "mimeType": "application/pdf",
        "size": "1024",
        "webViewLink": f"https://drive.example/{name}",
    }


class ServiceMonthMappingTest(unittest.TestCase):
    def test_usage_month_maps_to_each_services_transaction_month(self) -> None:
        expected = {
            "epos": "2026-06",
            "commufa": "2026-08",
            "tokuten": "2026-08",
            "mobile": "2026-07",
        }

        for service_id, transaction_month in expected.items():
            with self.subTest(service_id=service_id):
                self.assertEqual(
                    expected_transaction_month(service_id, "2026-07"),
                    transaction_month,
                )

    def test_transaction_month_maps_back_to_each_services_usage_month(self) -> None:
        cases = {
            "epos": ("2026-06", "2026-07"),
            "commufa": ("2026-08", "2026-07"),
            "tokuten": ("2026-08", "2026-07"),
            "mobile": ("2026-07", "2026-07"),
        }

        for service_id, (transaction_month, usage_month) in cases.items():
            with self.subTest(service_id=service_id):
                self.assertEqual(
                    usage_month_for_transaction(service_id, transaction_month),
                    usage_month,
                )

    def test_month_mapping_crosses_year_boundaries(self) -> None:
        self.assertEqual(expected_transaction_month("epos", "2026-01"), "2025-12")
        self.assertEqual(usage_month_for_transaction("epos", "2025-12"), "2026-01")

        for service_id in ("commufa", "tokuten"):
            with self.subTest(service_id=service_id):
                self.assertEqual(
                    expected_transaction_month(service_id, "2026-12"),
                    "2027-01",
                )
                self.assertEqual(
                    usage_month_for_transaction(service_id, "2027-01"),
                    "2026-12",
                )

        self.assertEqual(expected_transaction_month("mobile", "2026-12"), "2026-12")
        self.assertEqual(usage_month_for_transaction("mobile", "2026-12"), "2026-12")

    def test_real_drive_samples_are_stored_for_their_usage_months(self) -> None:
        samples = {
            "epos": ("20260605_株式会社エポスカード_10001円.pdf", "2026-07"),
            "commufa": ("20260611_中部テレコミュニケーション株式会社_10002円.pdf", "2026-05"),
            "tokuten": ("20260621_フラットエナジー株式会社_10003円.pdf", "2026-05"),
            "mobile": ("20260609_NTTファイナンス株式会社_10004円.pdf", "2026-06"),
        }

        for service_id, (file_name, usage_month) in samples.items():
            with self.subTest(service_id=service_id, usage_month=usage_month):
                files = [drive_file(file_name)]
                service = service_by_id(service_id)

                receipt = find_receipt(files, service, usage_month)

                self.assertIsNotNone(receipt)
                self.assertEqual(
                    receipt_month_state(files, service, usage_month),
                    ReceiptMonthState.STORED,
                )

    def test_real_drive_samples_do_not_match_a_different_usage_month(self) -> None:
        cases = {
            "epos": ("20260605_株式会社エポスカード_10001円.pdf", "2026-06"),
            "commufa": ("20260611_中部テレコミュニケーション株式会社_10002円.pdf", "2026-06"),
            "tokuten": ("20260621_フラットエナジー株式会社_10003円.pdf", "2026-06"),
            "mobile": ("20260609_NTTファイナンス株式会社_10004円.pdf", "2026-05"),
        }

        for service_id, (file_name, wrong_usage_month) in cases.items():
            with self.subTest(service_id=service_id, usage_month=wrong_usage_month):
                files = [drive_file(file_name)]
                service = service_by_id(service_id)

                self.assertIsNone(find_receipt(files, service, wrong_usage_month))
                self.assertEqual(
                    receipt_month_state(files, service, wrong_usage_month),
                    ReceiptMonthState.MISSING,
                )

    def test_epos_fetcher_converts_usage_month_to_payment_month(self) -> None:
        browser = Mock()
        fetcher = EposAutoFetcher(browser)
        form = {
            "action": "https://example.test/pdf",
            "pageUrl": "https://example.test/detail",
            "metadataText": "エポスカード 2026年6月 ご利用明細",
            "logs": [],
        }

        with (
            patch.object(fetcher, "_wait_for_login"),
            patch.object(fetcher, "_prepare_pdf_form", return_value=form) as prepare,
            patch.object(fetcher, "_post_pdf_form_in_chrome", return_value=b"%PDF-1.7 test"),
        ):
            statement = fetcher.fetch_pdf("2026-07")

        prepare.assert_called_once_with(2026, 6)
        self.assertEqual(statement.original_file_name, "epos_2026-06.pdf")

    def test_epos_synthetic_output_name_cannot_prove_statement_month(self) -> None:
        browser = Mock()
        fetcher = EposAutoFetcher(browser)
        form = {
            "action": "https://example.test/pdf",
            "pageUrl": "https://example.test/detail",
            "metadataText": "エポスカード ご利用明細",
            "logs": [],
        }

        with (
            patch.object(fetcher, "_wait_for_login"),
            patch.object(fetcher, "_prepare_pdf_form", return_value=form),
            patch.object(
                fetcher,
                "_post_pdf_form_in_chrome",
                return_value="%PDF-1.7\nエポスカード 2026年5月\n".encode(),
            ),
            self.assertRaises(AcquisitionError) as raised,
        ):
            fetcher.fetch_pdf("2026-07")

        self.assertEqual(
            "EPOS_PAYMENT_MONTH_MISMATCH",
            getattr(raised.exception, "code", ""),
        )

    def test_tokuten_search_uses_following_month_for_usage_month(self) -> None:
        self.assertEqual(build_tokuten_search_query("2026-07"), "トクテン 2026年8月")


if __name__ == "__main__":
    unittest.main()
