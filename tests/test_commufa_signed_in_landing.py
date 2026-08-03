"""Arriving already signed in must count as signed in.

Myコミュファ is a Salesforce Experience site. Once the browser profile is
remembered, the acquisition lands on the members' landing page instead of the
login form - and that page carries none of the words the configured hints look
for. It greets the member by name and shows "2026年6月のご利用料金", not 請求額
or ご契約内容, so the login wait ran its full length and reported LOGIN_TIMEOUT
while the owner was plainly signed in.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.official_sites import (  # noqa: E402
    CommufaAutoFetcher,
    _inside_commufa_members_area,
)


# Taken from the page the acquisition actually reported as a failure.
LANDING_TEXT = (
    "ナビゲーションへスキップ メインコンテンツへスキップ Myコミュファ ホーム "
    "マイページ 家族招待 家族情報 お問い合わせ 飯野 海斗 2026年6月のご利用料金 "
    "6,710 円 詳しくはこちら ※iPhone等をご利用の方でポップアップブロックが"
    "有効になっている場合に、コミュポンのクリックができない場合があります。"
)
LANDING = {
    "url": "https://mypage.commufa.jp/join/s/",
    "title": "Myコミュファ",
    "text": LANDING_TEXT,
    "passwordFields": 0,
}


class _Browser:
    def __init__(self, summaries: list[dict]) -> None:
        self._summaries = list(summaries)
        self.summary_calls = 0
        self.evaluated: list[str] = []

    def page_summary(self) -> dict:
        self.summary_calls += 1
        if len(self._summaries) > 1:
            return self._summaries.pop(0)
        return dict(self._summaries[0])

    def evaluate(self, expression: str, **_kwargs):
        self.evaluated.append(expression)
        return {
            "attempted": False,
            "code": "LOGIN_CONTROL_NOT_FOUND",
            "reason": "自動ログイン対象の入力欄またはボタンを見つけられませんでした。",
        }


class MembersAreaDetectionTest(unittest.TestCase):
    def test_the_signed_in_landing_page_is_recognised(self) -> None:
        self.assertTrue(_inside_commufa_members_area(LANDING))

    def test_the_login_page_is_not(self) -> None:
        self.assertFalse(
            _inside_commufa_members_area(
                {
                    "url": "https://mypage.commufa.jp/join/s/login/",
                    "text": "Myコミュファログイン ログインID（メールアドレス） パスワード",
                    "passwordFields": 1,
                }
            )
        )

    def test_the_verification_step_is_not(self) -> None:
        self.assertFalse(
            _inside_commufa_members_area(
                {
                    "url": "https://mypage.commufa.jp/join/s/identity/verification",
                    "text": "確認コード入力 コードを再送信",
                    "passwordFields": 0,
                }
            )
        )

    def test_another_host_is_never_accepted(self) -> None:
        """A redirect somewhere else must never look like a cleared sign-in."""

        self.assertFalse(
            _inside_commufa_members_area(
                {
                    "url": "https://mypage.commufa.jp.evil.example/join/s/",
                    "text": LANDING_TEXT,
                    "passwordFields": 0,
                }
            )
        )
        self.assertFalse(
            _inside_commufa_members_area(
                {"url": "http://mypage.commufa.jp/join/s/", "text": LANDING_TEXT, "passwordFields": 0}
            )
        )

    def test_a_blank_shell_is_not_yet_anything(self) -> None:
        self.assertFalse(
            _inside_commufa_members_area(
                {"url": "https://mypage.commufa.jp/join/s/", "text": "", "passwordFields": 0}
            )
        )

    def test_a_menu_entry_mentioning_login_does_not_undo_it(self) -> None:
        """Judged on the shape of the site, not on wording.

        The configured hints are matched by substring, so a members' menu
        offering "ログイン情報の変更" would otherwise read as a login page and
        bring the ninety-second hang straight back.
        """

        self.assertTrue(
            _inside_commufa_members_area(
                {
                    "url": "https://mypage.commufa.jp/join/s/",
                    "text": LANDING_TEXT + " ログイン情報の変更 ログアウト",
                    "passwordFields": 0,
                }
            )
        )


class LoginWaitTest(unittest.TestCase):
    def test_the_wait_ends_at_once_on_the_landing_page(self) -> None:
        browser = _Browser([LANDING])
        fetcher = CommufaAutoFetcher(browser)  # type: ignore[arg-type]

        with patch("src.automation.official_sites.time.sleep"):
            fetcher._wait_for_login(timeout_seconds=90)

        # And without ever running the credential script: the password is not
        # typed into a page that is already past the gate.
        self.assertEqual([], browser.evaluated)
        self.assertEqual(1, browser.summary_calls)

    def test_a_login_page_still_drives_the_sign_in(self) -> None:
        login = {
            "url": "https://mypage.commufa.jp/join/s/login/",
            "title": "Myコミュファ",
            "text": "Myコミュファログイン ログインID メールアドレス パスワード",
            "passwordFields": 1,
        }
        browser = _Browser([login])
        fetcher = CommufaAutoFetcher(browser, {"login_id": "x", "password": "y"})  # type: ignore[arg-type]

        with patch("src.automation.official_sites.time.sleep"):
            with self.assertRaises(Exception) as raised:
                fetcher._wait_for_login(timeout_seconds=0.5)

        self.assertTrue(browser.evaluated, "ログイン処理が試みられていない")
        self.assertEqual("LOGIN_REQUIRED", getattr(raised.exception, "code", ""))


if __name__ == "__main__":
    unittest.main()
