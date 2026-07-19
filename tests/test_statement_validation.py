from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.statement_validation import inspect_acquired_statement  # noqa: E402


class StatementValidationTest(unittest.TestCase):
    def test_epos_requires_the_payment_month(self) -> None:
        valid = inspect_acquired_statement(
            service_id="epos",
            target_month="2026-07",
            content=b"%PDF-1.7\n",
            metadata_text="エポスカード ご利用明細 2026年6月",
        )
        wrong_month = inspect_acquired_statement(
            service_id="epos",
            target_month="2026-07",
            content=b"%PDF-1.7\n",
            metadata_text="エポスカード ご利用明細 2026年7月",
        )

        self.assertTrue(valid.valid)
        self.assertTrue(wrong_month.partner_found)
        self.assertFalse(wrong_month.month_found)

    def test_utility_accepts_usage_or_following_billing_month(self) -> None:
        for visible_month in ("2026年7月", "2026年8月"):
            with self.subTest(visible_month=visible_month):
                result = inspect_acquired_statement(
                    service_id="tokuten",
                    target_month="2026-07",
                    content=b"%PDF-1.7\n",
                    metadata_text=f"トクテンでんき 請求書 {visible_month}",
                )
                self.assertTrue(result.valid)

    def test_wrong_provider_is_rejected_even_when_month_matches(self) -> None:
        result = inspect_acquired_statement(
            service_id="mobile",
            target_month="2026-07",
            content=b"%PDF-1.7\n",
            metadata_text="別会社の請求書 2026年7月",
        )

        self.assertFalse(result.partner_found)
        self.assertTrue(result.month_found)
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
