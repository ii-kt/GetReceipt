from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.auth_challenges import (  # noqa: E402
    AuthChallengeClassification,
    AuthChallengeSubmissionError,
    AuthCodeValidationError,
    SERVICE_PROFILES,
    inspect_current_auth_challenge,
    submit_current_auth_code,
)


class FakeBrowser:
    def __init__(
        self,
        *,
        url: str,
        text: str = "本人確認",
        visible_inputs: list[dict[str, str]] | None = None,
        evaluations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.url = url
        self.summary_url = url
        self.text = text
        self.visible_inputs = visible_inputs or []
        self.evaluations = list(evaluations or [])
        self.calls: list[str] = []
        self.expressions: list[str] = []

    def current_page_target(self) -> dict[str, str]:
        self.calls.append("current_page_target")
        return {"targetId": "live-target", "type": "page", "url": self.url}

    def current_page_summary(self) -> dict[str, Any]:
        self.calls.append("current_page_summary")
        return {
            "url": self.summary_url,
            "title": "本人確認",
            "text": self.text,
            "passwordFields": 0,
            "visibleInputs": list(self.visible_inputs),
        }

    def evaluate_current_page(
        self,
        expression: str,
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        self.calls.append("evaluate_current_page")
        self.expressions.append(expression)
        if not self.evaluations:
            raise AssertionError("unexpected evaluation")
        result = self.evaluations.pop(0)
        if isinstance(result, Exception):
            raise result
        return dict(result)


class AuthChallengesTest(unittest.TestCase):
    def test_profiles_declare_hosts_lengths_patterns_and_label_hints(self) -> None:
        epos = SERVICE_PROFILES["epos"]
        commufa = SERVICE_PROFILES["commufa"]
        webbilling = SERVICE_PROFILES["webbilling"]

        self.assertEqual(("www.eposcard.co.jp",), epos.allowed_hosts)
        self.assertEqual((3, 3), (epos.min_length, epos.max_length))
        self.assertIsNotNone(epos.pattern.fullmatch("123"))
        self.assertIn("セキュリティコード", epos.input_label_hints)

        self.assertEqual(("mypage.commufa.jp",), commufa.allowed_hosts)
        self.assertEqual((6, 6), (commufa.min_length, commufa.max_length))
        self.assertIsNotNone(commufa.pattern.fullmatch("123456"))
        self.assertIn("確認コード", commufa.input_label_hints)

        self.assertIn("webbilling.ntt-finance.co.jp", webbilling.allowed_hosts)
        self.assertIn("id.smt.docomo.ne.jp", webbilling.allowed_hosts)
        self.assertEqual((4, 8), (webbilling.min_length, webbilling.max_length))
        self.assertIsNotNone(webbilling.pattern.fullmatch("12345678"))
        self.assertIn("ワンタイムパスワード", webbilling.input_label_hints)

    def test_codes_are_ascii_digits_with_profile_specific_lengths(self) -> None:
        cases = (
            ("epos", "12", "https://www.eposcard.co.jp/member/"),
            ("epos", "１２３", "https://www.eposcard.co.jp/member/"),
            ("commufa", "12345x", "https://mypage.commufa.jp/join/s/"),
            ("webbilling", "123", "https://webbilling.ntt-finance.co.jp/mem/"),
            ("webbilling", "123456789", "https://webbilling.ntt-finance.co.jp/mem/"),
        )
        for service_id, rejected, url in cases:
            with self.subTest(service_id=service_id, rejected=rejected):
                browser = FakeBrowser(url=url)
                with self.assertRaises(AuthCodeValidationError) as caught:
                    submit_current_auth_code(browser, service_id, rejected)
                self.assertNotIn(rejected, str(caught.exception))
                self.assertNotIn(rejected, repr(caught.exception))
                self.assertEqual([], browser.calls)

    def test_only_exact_allowed_https_hosts_are_accepted(self) -> None:
        rejected_urls = (
            "http://mypage.commufa.jp/join/s/",
            "https://sub.mypage.commufa.jp/join/s/",
            "https://mypage.commufa.jp.evil.example/join/s/",
            "https://user@mypage.commufa.jp/join/s/",
            "https://mypage.commufa.jp:444/join/s/",
        )
        for url in rejected_urls:
            with self.subTest(url=url):
                browser = FakeBrowser(url=url)
                with self.assertRaises(AuthChallengeSubmissionError):
                    submit_current_auth_code(browser, "commufa", "123456")
                self.assertEqual(
                    ["current_page_target", "current_page_summary"],
                    browser.calls,
                )
                self.assertEqual([], browser.expressions)

    def test_target_and_summary_must_still_be_the_same_current_page(self) -> None:
        browser = FakeBrowser(url="https://mypage.commufa.jp/join/s/")
        browser.summary_url = "https://mypage.commufa.jp/join/other/"

        with self.assertRaises(AuthChallengeSubmissionError):
            submit_current_auth_code(browser, "commufa", "123456")

        self.assertEqual([], browser.expressions)

    def test_summary_captcha_blocks_before_any_code_reaches_evaluation(self) -> None:
        code = "123456"
        browser = FakeBrowser(
            url="https://mypage.commufa.jp/join/s/",
            text="私はロボットではありません CAPTCHA",
        )

        with self.assertRaises(AuthChallengeSubmissionError) as caught:
            submit_current_auth_code(browser, "commufa", code)

        self.assertEqual("interactive", caught.exception.classification)
        self.assertEqual([], browser.expressions)
        self.assertNotIn(code, str(caught.exception))
        self.assertNotIn(code, repr(caught.exception))

    def test_dom_probe_captcha_never_runs_the_code_submission_evaluation(self) -> None:
        code = "123456"
        browser = FakeBrowser(
            url="https://mypage.commufa.jp/join/s/",
            evaluations=[
                {
                    "classification": "interactive",
                    "inputCount": 0,
                    "submitCount": 0,
                }
            ],
        )

        with self.assertRaises(AuthChallengeSubmissionError) as caught:
            submit_current_auth_code(browser, "commufa", code)

        self.assertEqual("interactive", caught.exception.classification)
        self.assertEqual(1, len(browser.expressions))
        self.assertNotIn(code, browser.expressions[0])

    def test_passkey_is_classified_unsupported_and_never_automated(self) -> None:
        browser = FakeBrowser(
            url="https://id.smt.docomo.ne.jp/login",
            text="パスキーで認証してください",
        )

        observation = inspect_current_auth_challenge(browser, "webbilling")
        self.assertEqual(
            AuthChallengeClassification.UNSUPPORTED,
            observation.classification,
        )
        self.assertEqual([], browser.expressions)

        with self.assertRaises(AuthChallengeSubmissionError) as caught:
            submit_current_auth_code(browser, "webbilling", "123456")
        self.assertEqual("unsupported", caught.exception.classification)
        self.assertEqual([], browser.expressions)

    def test_input_candidate_must_be_exactly_one(self) -> None:
        for count in (0, 2):
            with self.subTest(count=count):
                browser = FakeBrowser(
                    url="https://mypage.commufa.jp/join/s/",
                    evaluations=[
                        {
                            "classification": "code_input",
                            "inputCount": count,
                            "submitCount": 1,
                        }
                    ],
                )
                with self.assertRaises(AuthChallengeSubmissionError):
                    submit_current_auth_code(browser, "commufa", "123456")
                self.assertEqual(1, len(browser.expressions))
                self.assertNotIn("123456", browser.expressions[0])

    def test_probe_without_code_controls_is_classified_as_none(self) -> None:
        browser = FakeBrowser(
            url="https://www.eposcard.co.jp/member/",
            evaluations=[
                {
                    "classification": "code_input",
                    "inputCount": 0,
                    "submitCount": 0,
                }
            ],
        )
        observation = inspect_current_auth_challenge(browser, "epos")
        self.assertEqual(AuthChallengeClassification.NONE, observation.classification)

    def test_submit_candidate_must_be_exactly_one(self) -> None:
        for count in (0, 2):
            with self.subTest(count=count):
                browser = FakeBrowser(
                    url="https://mypage.commufa.jp/join/s/",
                    evaluations=[
                        {
                            "classification": "code_input",
                            "inputCount": 1,
                            "submitCount": count,
                        }
                    ],
                )
                with self.assertRaises(AuthChallengeSubmissionError):
                    submit_current_auth_code(browser, "commufa", "123456")
                self.assertEqual(1, len(browser.expressions))
                self.assertNotIn("123456", browser.expressions[0])

    def test_success_uses_only_live_current_page_contract_and_never_echoes_code(self) -> None:
        code = "123456"
        browser = FakeBrowser(
            url="https://mypage.commufa.jp/join/s/",
            evaluations=[
                {
                    "classification": "code_input",
                    "inputCount": 1,
                    "submitCount": 1,
                },
                {"ok": True, "classification": "code_input"},
            ],
        )

        result = submit_current_auth_code(browser, "commufa", code)

        self.assertTrue(result.submitted)
        self.assertEqual("commufa", result.service_id)
        self.assertNotIn(code, repr(result))
        self.assertEqual(
            [
                "current_page_target",
                "current_page_summary",
                "evaluate_current_page",
                "evaluate_current_page",
            ],
            browser.calls,
        )

    def test_browser_exception_diagnostics_never_echo_code(self) -> None:
        code = "123456"
        browser = FakeBrowser(
            url="https://mypage.commufa.jp/join/s/",
            evaluations=[
                {
                    "classification": "code_input",
                    "inputCount": 1,
                    "submitCount": 1,
                },
                RuntimeError(f"evaluation failed for {code}"),
            ],
        )

        with self.assertRaises(AuthChallengeSubmissionError) as caught:
            submit_current_auth_code(browser, "commufa", code)

        self.assertNotIn(code, str(caught.exception))
        self.assertNotIn(code, repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
