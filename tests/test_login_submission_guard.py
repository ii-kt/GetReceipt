from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.epos import EposAutoFetcher  # noqa: E402
from src.automation.official_sites import (  # noqa: E402
    CommufaAutoFetcher,
    WebBillingAutoFetcher,
)


class _Browser:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []

    def click_at(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def press_key(self, key: str) -> None:
        self.keys.append(key)


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
        browser = _Browser()
        fetcher = EposAutoFetcher(browser)  # type: ignore[arg-type]
        layout = {"buttonPoint": {"x": 30, "y": 40}}

        fetcher._submit_epos_login_button(layout)
        fetcher._submit_epos_login_button(layout)

        self.assertEqual([(30, 40)], browser.clicks)

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
