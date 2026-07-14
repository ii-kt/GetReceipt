from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.workflows.receipt_archive import (  # noqa: E402
    ReceiptCurrency,
    archive_months,
    build_receipt_archive,
    duplicate_file_names,
    filter_receipts,
)


def drive_file(
    name: str,
    *,
    file_id: str | None = None,
    size: int | str | None = "1024",
    mime_type: str = "application/pdf",
) -> dict[str, object]:
    file: dict[str, object] = {
        "id": file_id or f"drive:{name}",
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": "2026-07-14T12:00:00Z",
        "webViewLink": f"https://drive.example/{file_id or name}",
    }
    if size is not None:
        file["size"] = size
    return file


class ReceiptArchiveTest(unittest.TestCase):
    def test_full_scale_anonymous_snapshot_preserves_23_24_1_classification(self) -> None:
        recurring_files = [
            *[
                drive_file(
                    f"{transaction_date}_株式会社エポスカード_{80000 + index}円.pdf",
                    file_id=f"epos-{index}",
                )
                for index, transaction_date in enumerate(
                    ("20251205", "20260106", "20260205", "20260305", "20260405", "20260505", "20260605"),
                    start=1,
                )
            ],
            *[
                drive_file(
                    f"{transaction_date}_中部テレコミュニケーション株式会社_{6000 + index}円.pdf",
                    file_id=f"commufa-{index}",
                )
                for index, transaction_date in enumerate(
                    ("20260211", "20260311", "20260411", "20260511", "20260611"),
                    start=1,
                )
            ],
            *[
                drive_file(
                    f"{transaction_date}_フラットエナジー株式会社_{7000 + index}円.pdf",
                    file_id=f"tokuten-{index}",
                )
                for index, transaction_date in enumerate(
                    ("20260212", "20260312", "20260412", "20260512", "20260612"),
                    start=1,
                )
            ],
            *[
                drive_file(
                    f"{transaction_date}_NTTファイナンス株式会社_{4000 + index}円.pdf",
                    file_id=f"mobile-{index}",
                )
                for index, transaction_date in enumerate(
                    ("20260109", "20260209", "20260309", "20260409", "20260509", "20260609"),
                    start=1,
                )
            ],
        ]
        one_off_files = [
            drive_file("20260530_Anthropic_$10-$10.pdf", file_id="one-off-usd-refund"),
            drive_file("20260530_OpenAI_1000円-1000円.pdf", file_id="one-off-jpy-refund"),
            *[
                drive_file(
                    f"202607{day:02d}_単発取引先{day:02d}_{1000 + day}円.pdf",
                    file_id=f"one-off-{day:02d}",
                )
                for day in range(1, 23)
            ],
        ]
        policy_document = {
            "id": "policy-document",
            "name": "電子取引データの事務処理規程",
            "mimeType": "application/vnd.google-apps.document",
        }
        files = [*recurring_files, *one_off_files, policy_document]

        archive = build_receipt_archive(files)

        self.assertEqual(len(files), 48)
        self.assertEqual(len(recurring_files), 23)
        self.assertEqual(len(archive.receipts), 24)
        self.assertEqual(archive.review_files, ())
        self.assertEqual(archive.ignored_non_pdf_count, 1)
        self.assertEqual(
            {
                receipt.file_name
                for receipt in archive.receipts
                if receipt.partner_name in {"Anthropic", "OpenAI"}
            },
            {
                "20260530_Anthropic_$10-$10.pdf",
                "20260530_OpenAI_1000円-1000円.pdf",
            },
        )

    def test_current_drive_formats_classify_recurring_and_one_off_receipts(self) -> None:
        files = [
            drive_file("20260605_株式会社エポスカード_10001円.pdf"),
            drive_file("20260611_中部テレコミュニケーション株式会社_10002円.pdf"),
            drive_file("20260621_フラットエナジー株式会社_10003円.pdf"),
            drive_file("20260609_NTTファイナンス株式会社_10004円.pdf"),
            drive_file("２０２６０６０５_株式会社 エポスカード_１０００１円.ＰＤＦ"),
            drive_file("20260530_Anthropic_$10-$10.pdf"),
            drive_file("20260530_OpenAI_1000円-1000円.pdf"),
            drive_file("20260708_交通サービス_500円.PDF", mime_type="application/octet-stream"),
            {
                "id": "policy",
                "name": "電子取引データに関する事務処理規程",
                "mimeType": "application/vnd.google-apps.document",
            },
        ]

        archive = build_receipt_archive(files)

        self.assertEqual(
            [receipt.file_name for receipt in archive.receipts],
            [
                "20260708_交通サービス_500円.PDF",
                "20260530_Anthropic_$10-$10.pdf",
                "20260530_OpenAI_1000円-1000円.pdf",
            ],
        )
        self.assertEqual(archive.review_files, ())
        self.assertEqual(archive.ignored_non_pdf_count, 1)

        anthropic = archive.receipts[1]
        self.assertEqual(anthropic.currency, ReceiptCurrency.USD)
        self.assertEqual(anthropic.amount_label, "$10-$10")
        self.assertEqual(anthropic.charged_amount, Decimal("10"))
        self.assertEqual(anthropic.refund_amount, Decimal("10"))
        self.assertEqual(anthropic.net_amount, Decimal("0"))
        self.assertTrue(anthropic.is_refund)

        openai = archive.receipts[2]
        self.assertEqual(openai.currency, ReceiptCurrency.JPY)
        self.assertEqual(openai.amount_label, "1000円-1000円")
        self.assertEqual(openai.net_amount, Decimal("0"))

    def test_valid_recurring_candidates_are_excluded_and_yenless_legacy_file_is_reviewed(self) -> None:
        files = [
            drive_file(
                "20261205_株式会社エポスカード_10001円.pdf",
                file_id="future-rent-a",
            ),
            drive_file(
                "20261205_株式会社エポスカード_10001円.pdf",
                file_id="future-rent-b",
            ),
            drive_file(
                "20260711_中部テレコミュニケーション株式会社_6710.pdf",
                file_id="legacy-commufa",
            ),
        ]

        archive = build_receipt_archive(files)

        self.assertEqual(archive.receipts, ())
        self.assertEqual(
            [review.file_id for review in archive.review_files],
            ["legacy-commufa"],
        )

    def test_known_recurring_issuer_never_leaks_into_one_off_archive(self) -> None:
        files = [
            drive_file("20260701_株式会社エポスカード_$22.pdf", file_id="known-usd"),
            drive_file("20260701_NTTファイナンス株式会社_3000円-3000円.pdf", file_id="known-refund"),
            drive_file("20260701_フラットエナジー株式会社_bad.pdf", file_id="known-malformed"),
        ]

        archive = build_receipt_archive(files)

        self.assertEqual(archive.receipts, ())
        self.assertEqual(len(archive.review_files), 3)
        self.assertTrue(
            all("月次サービス" in review.reason for review in archive.review_files)
        )

    def test_malformed_and_zero_size_pdfs_go_to_review_while_non_pdfs_are_ignored(self) -> None:
        files = [
            drive_file("not-a-receipt.pdf", file_id="malformed"),
            drive_file("20260230_OpenAI_$22.pdf", file_id="invalid-date"),
            drive_file("20260701__3000円.pdf", file_id="empty-partner"),
            drive_file("20260701_OpenAI_$22.pdf", file_id="zero", size=0),
            drive_file("20260701_OpenAI_$22.pdf", file_id="missing", size=None),
            drive_file("notes.txt", file_id="text", size=20, mime_type="text/plain"),
        ]

        archive = build_receipt_archive(files)

        self.assertEqual(archive.receipts, ())
        self.assertEqual(len(archive.review_files), 5)
        self.assertEqual(archive.ignored_non_pdf_count, 1)

    def test_currency_parsing_supports_regular_yen_usd_eur_and_gbp(self) -> None:
        files = [
            drive_file("20260704_JPY Shop_1,250円.pdf"),
            drive_file("20260703_US Shop_$22.50.pdf"),
            drive_file("20260702_EU Shop_€10.50-€2.25.pdf"),
            drive_file("20260701_UK Shop_12£-2£.pdf"),
        ]

        archive = build_receipt_archive(files)

        self.assertEqual(
            [receipt.currency for receipt in archive.receipts],
            [
                ReceiptCurrency.JPY,
                ReceiptCurrency.USD,
                ReceiptCurrency.EUR,
                ReceiptCurrency.GBP,
            ],
        )
        self.assertEqual(archive.receipts[0].charged_amount, Decimal("1250"))
        self.assertEqual(archive.receipts[2].net_amount, Decimal("8.25"))
        self.assertEqual(archive.receipts[3].amount_label, "12£-2£")

    def test_same_name_different_ids_are_preserved_and_reported(self) -> None:
        name = "20260710_OpenAI_$22.pdf"
        archive = build_receipt_archive(
            [
                drive_file(name, file_id="duplicate-a"),
                drive_file(name, file_id="duplicate-b"),
            ]
        )

        self.assertEqual([receipt.file_id for receipt in archive.receipts], ["duplicate-a", "duplicate-b"])
        self.assertEqual(duplicate_file_names(archive.receipts), (name,))

    def test_archive_helpers_filter_without_changing_deterministic_order(self) -> None:
        archive = build_receipt_archive(
            [
                drive_file("20260601_OpenAI_$20.pdf", file_id="june-usd"),
                drive_file("20260703_Anthropic_$22-$5.pdf", file_id="july-refund"),
                drive_file("20260702_小売サービス_1200円.pdf", file_id="july-jpy"),
                drive_file("20260501_OpenAI_$10.pdf", file_id="may-usd"),
            ]
        )

        self.assertEqual(archive_months(archive.receipts), ("2026-07", "2026-06", "2026-05"))
        self.assertEqual(
            [receipt.file_id for receipt in filter_receipts(archive.receipts, query="openai")],
            ["june-usd", "may-usd"],
        )
        self.assertEqual(
            [receipt.file_id for receipt in filter_receipts(archive.receipts, month="2026-07")],
            ["july-refund", "july-jpy"],
        )
        self.assertEqual(
            [receipt.file_id for receipt in filter_receipts(archive.receipts, currency="usd")],
            ["july-refund", "june-usd", "may-usd"],
        )
        self.assertEqual(
            [receipt.file_id for receipt in filter_receipts(archive.receipts, refund="返金のみ")],
            ["july-refund"],
        )
        self.assertEqual(
            [receipt.file_id for receipt in filter_receipts(archive.receipts, refund="standard")],
            ["july-jpy", "june-usd", "may-usd"],
        )


if __name__ == "__main__":
    unittest.main()
