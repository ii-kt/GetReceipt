from __future__ import annotations

import inspect
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.epos import (  # noqa: E402
    AcquisitionError,
    EposAutoFetcher,
    FetchedStatement,
    build_epos_auto_login_expression,
)
from src.automation.official_sites import (  # noqa: E402
    WebBillingAutoFetcher,
    build_webbilling_auto_login_expression,
)


def statement() -> FetchedStatement:
    return FetchedStatement(
        content=b"%PDF-1.7 test",
        source_url="https://provider.example/",
        original_file_name="receipt.pdf",
        metadata_text="",
        logs=(),
    )


class ProviderChallengeResumeTest(unittest.TestCase):
    def test_epos_and_webbilling_scripts_expose_only_code_challenges_as_resumable(self) -> None:
        epos = build_epos_auto_login_expression(
            {"login_id": "configured", "password": "configured"}
        )
        webbilling = build_webbilling_auto_login_expression(
            {"dAccountId": "configured", "password": "configured"}
        )

        self.assertIn('challengeKind: "verification_code"', epos)
        self.assertIn('location.hostname === "www.eposcard.co.jp"', epos)
        self.assertIn('challengeKind: "captcha"', epos)
        self.assertIn('challengeKind: "verification_code"', webbilling)
        self.assertIn('"webbilling.ntt-finance.co.jp", "id.smt.docomo.ne.jp"', webbilling)
        self.assertIn('challengeKind: "captcha"', webbilling)
        self.assertIn("パスキー", webbilling)

    def test_epos_resume_submits_to_current_page_before_normal_fetch(self) -> None:
        browser = MagicMock()
        fetcher = EposAutoFetcher(browser)
        expected = statement()

        with (
            patch("src.automation.epos.submit_current_auth_code") as submit,
            patch.object(fetcher, "_wait_for_login_after_security_code") as wait,
            patch.object(fetcher, "fetch_pdf", return_value=expected) as fetch,
        ):
            actual = fetcher.resume_after_security_code("2026-07", "123")

        self.assertIs(expected, actual)
        submit.assert_called_once_with(browser, "epos", "123")
        wait.assert_called_once_with()
        fetch.assert_called_once_with("2026-07")

    def test_webbilling_resume_submits_to_current_page_before_normal_fetch(self) -> None:
        browser = MagicMock()
        fetcher = WebBillingAutoFetcher(browser)
        expected = statement()

        with (
            patch("src.automation.official_sites.submit_current_auth_code") as submit,
            patch.object(fetcher, "_wait_for_login_after_security_code") as wait,
            patch.object(fetcher, "fetch_pdf", return_value=expected) as fetch,
        ):
            actual = fetcher.resume_after_security_code("2026-07", "123456")

        self.assertIs(expected, actual)
        submit.assert_called_once_with(browser, "webbilling", "123456")
        wait.assert_called_once_with()
        fetch.assert_called_once_with("2026-07")

    def test_epos_pdf_post_stays_inside_the_same_chrome_session(self) -> None:
        browser = MagicMock()
        content = b"%PDF-1.7 same Chrome"
        browser.evaluate.return_value = {
            "ok": True,
            "status": 200,
            "responseUrl": "https://www.eposcard.co.jp/memberservice/pdf",
            "contentType": "application/pdf",
            "base64": base64.b64encode(content).decode("ascii"),
        }
        fetcher = EposAutoFetcher(browser)

        actual = fetcher._post_pdf_form_in_chrome(
            {
                "action": "https://www.eposcard.co.jp/memberservice/pdf",
                "pageUrl": "https://www.eposcard.co.jp/memberservice/detail",
                "fields": [["year", "2026"], ["month", "06"]],
            }
        )

        self.assertEqual(content, actual)
        browser.evaluate.assert_called_once()
        expression = browser.evaluate.call_args.args[0]
        self.assertIn('credentials: "include"', expression)
        self.assertIn("fetch(payload.action", expression)
        self.assertEqual(75, browser.evaluate.call_args.kwargs["timeout"])

    def test_epos_pdf_post_refuses_non_official_origin_before_chrome_evaluation(self) -> None:
        browser = MagicMock()
        fetcher = EposAutoFetcher(browser)
        with self.assertRaises(AcquisitionError) as raised:
            fetcher._post_pdf_form_in_chrome(
                {
                    "action": "https://www.eposcard.co.jp.evil.example/pdf",
                    "fields": [],
                }
            )
        self.assertEqual("PDF_REQUEST_ORIGIN_MISMATCH", raised.exception.code)
        browser.evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()



class CommufaInPageNavigationTest(unittest.TestCase):
    """Commufa's portal re-renders, so navigation must click in the page.

    Measuring an element and clicking its coordinates a moment later silently
    misses on this single-page app, which is how the login button failed.
    """

    def test_every_commufa_navigation_click_is_activated_in_page(self) -> None:
        from src.automation.official_sites import build_commufa_step_expression

        expression = build_commufa_step_expression(2026, 6)

        # No navigation step may hand raw coordinates back for clicking.
        self.assertNotIn("click: pointOf(", expression)
        self.assertIn("const activate = (el) =>", expression)
        self.assertEqual(4, expression.count("clickedInPage: true"))

    def test_python_does_not_reclick_an_in_page_activation(self) -> None:
        from src.automation import official_sites

        source = inspect.getsource(
            official_sites.CommufaAutoFetcher._fetch_statement_from_current_page
        )
        clicked_marker = source.index('action.get("clickedInPage")')
        coordinate_marker = source.index('self.browser.click_at')
        # The in-page branch must be reached before the coordinate fallback.
        self.assertLess(clicked_marker, coordinate_marker)


class SecurityCodeGateTest(unittest.TestCase):
    """After the code, an interstitial must not look like a failure."""

    def _gate(self, summary):
        from src.automation.official_sites import _passed_security_code_gate

        return _passed_security_code_gate(summary)

    def test_interstitial_without_logged_in_markers_passes(self) -> None:
        self.assertTrue(
            self._gate(
                {
                    "url": "https://mypage.commufa.jp/join/s/",
                    "passwordFields": 0,
                    "text": "処理中です しばらくお待ちください",
                }
            )
        )

    def test_login_form_still_showing_does_not_pass(self) -> None:
        self.assertFalse(
            self._gate(
                {
                    "url": "https://mypage.commufa.jp/join/s/login/",
                    "passwordFields": 1,
                    "text": "ログイン",
                }
            )
        )

    def test_rejected_login_does_not_pass(self) -> None:
        self.assertFalse(
            self._gate(
                {
                    "url": "https://mypage.commufa.jp/join/s/",
                    "passwordFields": 0,
                    "text": "ログインに失敗しました。ユーザー名とパスワードが正しいかご確認ください。",
                }
            )
        )

    def test_offsite_page_does_not_pass(self) -> None:
        self.assertFalse(
            self._gate(
                {
                    "url": "https://example.com/",
                    "passwordFields": 0,
                    "text": "ようこそ",
                }
            )
        )
