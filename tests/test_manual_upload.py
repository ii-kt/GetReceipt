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


class ImageOnlyPdfIsNotMinedForNumbersTest(unittest.TestCase):
    """An image-only invoice must not yield an invented amount.

    A real electricity invoice with no text layer was saved as "29円" because
    the extractor decoded the PDF's own bytes - object numbers, stream
    lengths, transform matrices - and found a plausible figure there.
    """

    def _image_only_pdf(self) -> bytes:
        # Mirrors the provider's real file: a single page whose content is one
        # drawn image and no text operators at all.
        objects = [
            b"<</Type /Catalog /Pages 2 0 R>>",
            b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
            b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources <</XObject <</I0 5 0 R>>>> /Contents 4 0 R>>",
            b"<</Length 44>>\nstream\nq 595 0 0 842 0 0 cm /I0 Do Q\nendstream",
            b"<</Type /XObject /Subtype /Image /Width 8 /Height 8 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 64>>\n"
            b"stream\n" + b"\x80" * 64 + b"\nendstream",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for index, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n" % (len(objects) + 1)
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1,
            xref,
        )
        return bytes(out)

    def test_no_text_layer_yields_no_text(self) -> None:
        from src.domain.document_metadata import extract_pdf_text

        self.assertEqual("", extract_pdf_text(self._image_only_pdf()))

    def test_no_amount_is_invented_from_the_file_structure(self) -> None:
        from src.domain.document_metadata import extract_receipt_data

        extracted = extract_receipt_data(self._image_only_pdf())

        self.assertIsNone(extracted.amount_yen)

    def test_manual_upload_refuses_rather_than_guessing(self) -> None:
        with self.assertRaises(ManualUploadError):
            inspect_manual_receipt(
                service_id="tokuten",
                target_month="2026-06",
                content=self._image_only_pdf(),
            )


class ReceiptDatePrefersItsOwnMonthTest(unittest.TestCase):
    """An invoice lists several dates; only one identifies the receipt.

    A Tokuten invoice shows its issue date, the usage period and the reading
    dates. Without preferring the month the receipt belongs to, the earliest
    unrelated date won and the file was dated the first of the month.
    """

    def test_issue_date_wins_over_an_earlier_usage_period(self) -> None:
        from src.domain.document_metadata import extract_transaction_date

        text = (
            "請求書 発行日2026年7月18日 2026年6月ご利用分 "
            "ご使用期間20260507〜20260602"
        )

        self.assertEqual(
            "2026-07-18",
            str(extract_transaction_date(text, prefer_month="2026-07")),
        )
        # Without the hint the earliest candidate still wins, as before.
        self.assertEqual(
            "2026-05-07",
            str(extract_transaction_date(text)),
        )

    def test_unrelated_month_hint_does_not_discard_every_candidate(self) -> None:
        from src.domain.document_metadata import extract_transaction_date

        text = "発行日2026年7月18日"

        self.assertEqual(
            "2026-07-18",
            str(extract_transaction_date(text, prefer_month="2030-01")),
        )


class OcrRepairsMisreadCharactersTest(unittest.TestCase):
    """The OCR engine renders 円 as 月 and prefers simplified shapes."""

    def test_comma_grouped_number_followed_by_month_is_yen(self) -> None:
        from src.domain.document_metadata import _repair_ocr_text, extract_amount_yen

        repaired = _repair_ocr_text("請求额合計（税达） 7,615月")

        self.assertIn("7,615円", repaired)
        self.assertIn("請求額合計", repaired)
        self.assertEqual(7615, extract_amount_yen(repaired))

    def test_a_real_month_is_left_alone(self) -> None:
        from src.domain.document_metadata import _repair_ocr_text

        self.assertEqual("2026年6月ご利用分", _repair_ocr_text("2026年6月ご利用分"))


class DateNeedsItsDayMarkerTest(unittest.TestCase):
    """A digit from an amount must not be read as a day.

    An NTT certificate prints "2026年 6月分 4,882円 ... 2026年 6月 9日".
    A loose pattern read "6月分 4" as the 4th of June, and that false date
    beat the real payment date.
    """

    def test_amount_after_a_month_is_not_a_date(self) -> None:
        from src.domain.document_metadata import extract_transaction_date

        text = "支払年月日 記事 2026年 6月分 4,882円 2026年 6月 9日 ドコモご利用分"

        self.assertEqual(
            "2026-06-09",
            str(extract_transaction_date(text, prefer_month="2026-06")),
        )

    def test_full_width_statement_is_readable(self) -> None:
        from src.domain.document_metadata import (
            extract_amount_yen,
            extract_transaction_date,
        )

        text = "ご利用金額 ４，８８２円 支払年月日 ２０２６年 ６月 ９日"

        self.assertEqual(4882, extract_amount_yen(text))
        self.assertEqual("2026-06-09", str(extract_transaction_date(text)))
