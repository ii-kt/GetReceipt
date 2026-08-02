"""One attempt cannot outlive its budget.

Every wait in the automation is bounded on its own, but they compose: a
sign-in wait, then a step loop, then a download wait, then all of it again
after a verification code. Added up they reach tens of minutes, and on a phone
that is indistinguishable from an acquisition that never ends.

The budget is held by the browser because every one of those loops has to talk
to it to make progress, so none of them can quietly opt out - including loops
written later.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import (  # noqa: E402
    AcquisitionDeadlineExceeded,
    ManagedBrowser,
)
from src.automation.official_sites import WebBillingAutoFetcher  # noqa: E402
from src.workflows.auto_acquisition import (  # noqa: E402
    NOT_ISSUED_CODES,
    run_auto_acquisition,
)


class _Browser:
    """A browser that always reports the same page, as a stuck site would."""

    def __init__(self, action: dict) -> None:
        self.action = action
        self.evaluations = 0
        self.clicks: list[tuple[int, int]] = []
        self.download_dir = Path()

    def clear_downloads(self) -> None:
        pass

    def navigate(self, url: str, wait_seconds: float = 1.0) -> None:
        pass

    def evaluate(self, expression: str, **_kwargs):
        self.evaluations += 1
        return dict(self.action)

    def click_at(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def switch_to_page(self, predicate):
        return None

    def wait_for_download(self, *_args, **_kwargs):
        return None

    def page_summary(self) -> dict:
        return {"url": "https://webbilling.ntt-finance.co.jp/", "text": ""}


class BrowserBudgetTest(unittest.TestCase):
    def test_a_spent_budget_stops_the_next_browser_action(self) -> None:
        browser = ManagedBrowser()
        browser.set_deadline(0)

        for action in (
            lambda: browser.navigate("https://example.invalid/"),
            lambda: browser.evaluate("1"),
            lambda: browser.click_at(1, 1),
            lambda: browser.press_key("Enter"),
        ):
            with self.assertRaises(AcquisitionDeadlineExceeded):
                action()

    def test_a_single_call_cannot_outlast_what_is_left(self) -> None:
        browser = ManagedBrowser()
        browser.set_deadline(4)

        # A thirty-second call with four seconds of budget left would blow the
        # whole limit on its own.
        self.assertLessEqual(browser._bounded(30), 4)
        # Never zero: a call given no time reads as a browser fault instead of
        # a spent budget.
        browser.set_deadline(0.01)
        self.assertGreaterEqual(browser._bounded(30), 1.0)

    def test_no_budget_leaves_every_timeout_alone(self) -> None:
        """A browser held open for the owner is not on the clock."""

        browser = ManagedBrowser()
        browser.set_deadline(30)
        browser.set_deadline(None)

        self.assertIsNone(browser.remaining_seconds())
        self.assertEqual(30, browser._bounded(30))
        browser.evaluate  # attribute access only; nothing is dispatched

    def test_the_error_reports_a_spent_budget_rather_than_a_site_fault(self) -> None:
        error = AcquisitionDeadlineExceeded()

        self.assertEqual("ACQUISITION_TIMEOUT", error.code)


class StalledStepTest(unittest.TestCase):
    def test_a_click_that_never_progresses_is_abandoned(self) -> None:
        """Twenty-eight repeats of the same click only spend the whole budget."""

        browser = _Browser(
            {
                "ok": False,
                "code": "CLICK_SEARCH",
                "click": {"x": 5, "y": 6},
                "waitMs": 1200,
            }
        )
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]

        with patch.object(WebBillingAutoFetcher, "_wait_for_login", lambda self: None):
            with patch("src.automation.official_sites.time.sleep"):
                with self.assertRaises(Exception) as raised:
                    fetcher.fetch_pdf("2026-08")

        self.assertEqual("WEBBILLING_STEP_STALLED", getattr(raised.exception, "code", ""))
        self.assertLess(browser.evaluations, 10)

    def test_a_month_web_billing_has_not_issued_reads_as_unissued(self) -> None:
        """Not billed yet is the ordinary state of the current month."""

        browser = _Browser(
            {
                "ok": False,
                "code": "YEAR_MONTH_NOT_AVAILABLE",
                "availableMonths": ["2026/06", "2026/07"],
                "message": "見つかりません",
            }
        )
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]

        with patch.object(WebBillingAutoFetcher, "_wait_for_login", lambda self: None):
            with patch("src.automation.official_sites.time.sleep"):
                with self.assertRaises(Exception) as raised:
                    fetcher.fetch_pdf("2026-08")

        code = getattr(raised.exception, "code", "")
        self.assertEqual("WEBBILLING_MONTH_NOT_ISSUED", code)
        self.assertIn(code, NOT_ISSUED_CODES)
        self.assertIn("2026年7月分", str(getattr(raised.exception, "advice", "")))


class _Storage:
    def list_files(self) -> list[dict[str, str]]:
        return []

    def upsert_bytes(self, **_kwargs):
        raise AssertionError("nothing should be saved for a timed-out attempt")


class TimedOutAttemptTest(unittest.TestCase):
    def test_the_owner_is_told_the_time_ran_out_not_that_the_site_broke(self) -> None:
        class _Fetcher:
            def fetch_pdf(self, target_month: str):
                raise AcquisitionDeadlineExceeded()

        result = run_auto_acquisition(
            service_id="mobile",
            target_month="2026-08",
            fetcher=_Fetcher(),
            storage=_Storage(),
        )

        self.assertFalse(result.success)
        self.assertEqual("ACQUISITION_TIMEOUT", result.failure.code)
        self.assertIn("制限時間", result.failure.message)
        # A spent budget is not a month the provider never billed, so it must
        # not be filed away as one and left unretried.
        self.assertNotIn("ACQUISITION_TIMEOUT", NOT_ISSUED_CODES)


class BudgetHonouredEndToEndTest(unittest.TestCase):
    def test_a_provider_that_never_answers_still_ends_the_attempt(self) -> None:
        """The whole point: a silent site cannot hold the run open forever."""

        class _SlowBrowser(_Browser):
            def evaluate(self, expression: str, **_kwargs):
                self.evaluations += 1
                self._elapsed += 1.0
                if self._elapsed >= self._budget:
                    raise AcquisitionDeadlineExceeded()
                return {"ok": False, "code": "STEP", "continue": True, "waitMs": 900}

            _elapsed = 0.0
            _budget = 6.0

        browser = _SlowBrowser({})
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]
        started = time.monotonic()

        with patch.object(WebBillingAutoFetcher, "_wait_for_login", lambda self: None):
            with patch("src.automation.official_sites.time.sleep"):
                with self.assertRaises(AcquisitionDeadlineExceeded):
                    fetcher.fetch_pdf("2026-08")

        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
