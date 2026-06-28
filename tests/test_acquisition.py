from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.acquisition import (  # noqa: E402
    acquisition_guidance,
    default_transaction_date,
    epos_payment_option_hint,
)


class AcquisitionGuidanceTest(unittest.TestCase):
    def test_epos_payment_option_hint_uses_payment_month(self) -> None:
        self.assertEqual(epos_payment_option_hint("2026-01"), "2026年1月27日お支払分")

    def test_epos_default_transaction_date_uses_payment_day(self) -> None:
        self.assertEqual(default_transaction_date("epos", "2026-01"), date(2026, 1, 27))

    def test_generic_default_transaction_date_uses_today(self) -> None:
        self.assertEqual(
            default_transaction_date("commufa", "2026-01", today=date(2026, 6, 25)),
            date(2026, 6, 25),
        )

    def test_epos_guidance_mentions_pdf_and_target_option(self) -> None:
        guidance = acquisition_guidance("epos", "2026-01")
        self.assertEqual(guidance.target_hint, "2026年1月27日お支払分")
        self.assertTrue(any("PDFを自動取得" in step for step in guidance.steps))


if __name__ == "__main__":
    unittest.main()
