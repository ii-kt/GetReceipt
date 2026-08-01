from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.epos import AcquisitionError, EposAutoFetcher  # noqa: E402
from src.automation.official_sites import (  # noqa: E402
    CommufaAutoFetcher,
    WebBillingAutoFetcher,
)


class _Browser:
    def __init__(self, evaluations: list | None = None) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.evaluated: list[str] = []
        self._evaluations = list(evaluations or [])

    def click_at(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def clear_focused_text(self) -> None:
        pass

    def type_text(self, text: str, delay_seconds: float = 0.0) -> None:
        self.typed.append(text)

    def evaluate(self, expression: str, **_kwargs):
        self.evaluated.append(expression)
        return self._evaluations.pop(0) if self._evaluations else None


class LoginSubmissionGuardTest(unittest.TestCase):
    def test_epos_submits_credentials_only_once_per_browser_attempt(self) -> None:
        browser = _Browser()
        fetcher = EposAutoFetcher(browser)  # type: ignore[arg-type]
        result = {
            "attempted": True,
            "code": "SUBMIT_PASSWORD",
            "click": {"x": 10, "y": 20},
        }

        with patch("src.automation.epos.time.sleep"):
            self.assertTrue(fetcher._apply_login_result(result))
            # The password is never resent, but the attempt continues so a slow
            # provider page is not mistaken for a failure.
            self.assertFalse(fetcher._apply_login_result(result))

        self.assertEqual([(10, 20)], browser.clicks)

    def test_epos_human_layout_submit_has_the_same_limit(self) -> None:
        browser = _Browser([{"ok": True, "hit": True, "point": {"x": 30, "y": 40}}])
        fetcher = EposAutoFetcher(browser)  # type: ignore[arg-type]
        layout = {"buttonPoint": {"x": 999, "y": 999}}

        fetcher._submit_epos_login_button(layout)
        fetcher._submit_epos_login_button(layout)

        # The point is re-measured at click time: focusing the inputs scrolls
        # the page, so the point measured with the layout is already stale.
        self.assertEqual([(30, 40)], browser.clicks)

    def test_epos_submits_in_page_when_the_click_would_miss(self) -> None:
        """A click that lands behind the button must not strand the sign-in."""

        browser = _Browser(
            [{"ok": True, "hit": False, "point": {"x": 30, "y": 40}}, "login()"]
        )
        fetcher = EposAutoFetcher(browser)  # type: ignore[arg-type]

        fetcher._submit_epos_login_button({"buttonPoint": {"x": 30, "y": 40}})

        self.assertEqual([], browser.clicks)
        self.assertIn("noCardUseDetailLoginForm", browser.evaluated[-1])

    def test_epos_still_submits_only_once_through_the_in_page_route(self) -> None:
        browser = _Browser(
            [{"ok": False, "code": "EPOS_LOGIN_BUTTON_NOT_FOUND"}, "form.submit()"]
        )
        fetcher = EposAutoFetcher(browser)  # type: ignore[arg-type]

        fetcher._submit_epos_login_button({})
        fetcher._submit_epos_login_button({})

        submits = [e for e in browser.evaluated if "noCardUseDetailLoginForm" in e]
        self.assertEqual(1, len(submits))

    def test_epos_stops_retyping_the_password_while_it_waits(self) -> None:
        """Waiting must not mean refilling the form on every poll.

        The password can only be sent once per attempt, so re-typing it just
        delays noticing that the sign-in already worked, and puts the password
        back on the page again and again.
        """

        browser = _Browser([{"ok": True, "hit": True, "point": {"x": 1, "y": 2}}])
        browser.evaluate = lambda expression, **kwargs: (  # type: ignore[assignment]
            True
            if "activeElement" in expression
            else {"ok": True, "hit": True, "point": {"x": 1, "y": 2}}
            if "elementFromPoint" in expression
            else {"readyState": "complete", "hasLoginId": True, "hasPassword": True,
                  "hasAbckCookie": True, "hasBmCookie": True}
            if "readyState" in expression
            else {"ok": True, "buttonPoint": {"x": 1, "y": 2}}
            if "buttonPoint" in expression
            else {"loginIdMatches": True, "passwordMatches": True}
        )
        fetcher = EposAutoFetcher(
            browser,  # type: ignore[arg-type]
            credentials={"login_id": "kt0000000", "password": "0000000kt"},
        )

        with patch("src.automation.epos.time.sleep"):
            first = fetcher._perform_human_login_attempt()
            calls_after_first = len(browser.evaluated)
            second = fetcher._perform_human_login_attempt()
            third = fetcher._perform_human_login_attempt()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertFalse(third)
        # Waiting costs nothing: no page work and no retyping after the submit.
        self.assertEqual(calls_after_first, len(browser.evaluated))
        self.assertEqual(["kt0000000", "0000000kt"], browser.typed)
        self.assertEqual(1, len(browser.clicks))

    def test_commufa_submits_credentials_only_once(self) -> None:
        browser = _Browser()
        fetcher = CommufaAutoFetcher(browser)  # type: ignore[arg-type]
        result = {
            "attempted": True,
            "code": "SUBMIT_PASSWORD_ENTER",
            "pressEnter": True,
        }

        with patch("src.automation.official_sites.time.sleep"):
            self.assertTrue(fetcher._apply_login_result(result))
            # A second pass must not resend the password, and must not end the
            # acquisition: the provider may simply still be navigating.
            self.assertFalse(fetcher._apply_login_result(result))

        self.assertEqual(["Enter"], browser.keys)

    def test_webbilling_submits_credentials_only_once(self) -> None:
        browser = _Browser()
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]
        result = {
            "attempted": True,
            "code": "SUBMIT_PASSWORD",
            "click": {"x": 50, "y": 60},
        }

        with patch("src.automation.official_sites.time.sleep"):
            self.assertTrue(fetcher._apply_login_result(result))
            self.assertFalse(fetcher._apply_login_result(result))

        self.assertEqual([(50, 60)], browser.clicks)


if __name__ == "__main__":
    unittest.main()


class InPageSubmissionTest(unittest.TestCase):
    """A control the page already activated must not be clicked again."""

    def test_in_page_click_is_not_repeated_by_coordinates(self) -> None:
        browser = _Browser()
        fetcher = CommufaAutoFetcher(browser)  # type: ignore[arg-type]
        result = {
            "attempted": True,
            "code": "SUBMIT_PASSWORD",
            "clickedInPage": True,
            "click": {"x": 70, "y": 80},
        }

        with patch("src.automation.official_sites.time.sleep"):
            self.assertTrue(fetcher._apply_login_result(result))

        self.assertEqual([], browser.clicks)


class CredentialCommitPauseTest(unittest.TestCase):
    """Filling and submitting must not happen in the same page tick."""

    def test_fill_pass_reports_progress_without_clicking(self) -> None:
        browser = _Browser()
        fetcher = CommufaAutoFetcher(browser)  # type: ignore[arg-type]
        result = {
            "attempted": True,
            "code": "CREDENTIALS_FILLED",
            "filled": True,
        }

        with patch("src.automation.official_sites.time.sleep"):
            self.assertTrue(fetcher._apply_login_result(result))

        self.assertEqual([], browser.clicks)
        self.assertEqual([], browser.keys)
        # The fill pass must not consume the one-submission allowance.
        self.assertFalse(fetcher._credential_submission_attempted)

    def test_provider_rejection_stops_instead_of_retrying(self) -> None:
        browser = _Browser()
        fetcher = CommufaAutoFetcher(browser)  # type: ignore[arg-type]

        with self.assertRaises(AcquisitionError) as raised:
            fetcher._apply_login_result(
                {"attempted": False, "code": "LOGIN_REJECTED"}
            )

        self.assertEqual("LOGIN_REJECTED", raised.exception.code)
