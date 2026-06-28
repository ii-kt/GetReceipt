from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.document_metadata import extract_amount_yen, extract_receipt_data, extract_transaction_date  # noqa: E402


class DocumentMetadataTest(unittest.TestCase):
    def test_extract_transaction_date_prefers_payment_context(self) -> None:
        text = "2026年1月6日作成 お支払日 2026年1月27日 ご請求金額 90,000円"
        self.assertEqual(extract_transaction_date(text), date(2026, 1, 27))

    def test_extract_amount_yen_prefers_billing_context(self) -> None:
        text = "明細 2,000円 ご請求金額 90,000円"
        self.assertEqual(extract_amount_yen(text), 90000)

    def test_extract_receipt_data_uses_text_hints(self) -> None:
        extracted = extract_receipt_data(b"%PDF-1.4", "お支払日 2026年1月27日 ご請求金額 90,000円")
        self.assertEqual(extracted.transaction_date, date(2026, 1, 27))
        self.assertEqual(extracted.amount_yen, 90000)


if __name__ == "__main__":
    unittest.main()
