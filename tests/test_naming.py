from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.domain.naming import (  # noqa: E402
    ReceiptMetadata,
    build_receipt_filename,
    normalize_extension,
    safe_name_part,
)


class NamingTest(unittest.TestCase):
    def test_build_receipt_filename_normalizes_partner_and_extension(self) -> None:
        metadata = ReceiptMetadata(
            transaction_date=date(2026, 7, 1),
            partner_name="株式会社 NTT/ドコモ",
            amount_yen=8250,
        )

        self.assertEqual(
            build_receipt_filename(metadata, ".PDF"),
            "20260701_株式会社_NTT_ドコモ_8250円.pdf",
        )

    def test_filename_normalizers_handle_japanese_names(self) -> None:
        self.assertEqual(normalize_extension("明細.PDF"), "pdf")
        self.assertEqual(safe_name_part(" A  / B "), "A_B")


if __name__ == "__main__":
    unittest.main()
