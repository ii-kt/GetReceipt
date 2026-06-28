from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.official_site_automation import (  # noqa: E402
    SERVICE_AUTOMATION_CONFIGS,
    assert_commufa_usage_month,
    build_tokuten_search_query,
    classify_configured_login_state,
    classify_tokuten_login_state,
    target_lookup_month,
)


class OfficialSiteAutomationTest(unittest.TestCase):
    def test_tokuten_lookup_uses_next_month_mail(self) -> None:
        self.assertEqual(target_lookup_month("tokuten", "2026-01"), "2026-02")
        self.assertEqual(target_lookup_month("tokuten", "2026-12"), "2027-01")

    def test_non_tokuten_lookup_uses_selected_month(self) -> None:
        self.assertEqual(target_lookup_month("commufa", "2026-01"), "2026-01")
        self.assertEqual(target_lookup_month("mobile", "2026-01"), "2026-01")

    def test_tokuten_search_query_uses_lookup_month(self) -> None:
        self.assertEqual(build_tokuten_search_query("2026-01"), "トクテン 2026年2月")

    def test_tokuten_login_state_detects_outlook_mailbox(self) -> None:
        summary = {
            "url": "https://outlook.live.com/mail/0/",
            "title": "Outlook",
            "text": "受信トレイ 検索 メール",
            "passwordFields": 0,
            "visibleInputs": [{"text": "検索", "type": "search"}],
        }
        self.assertEqual(classify_tokuten_login_state(summary), "logged-in")

    def test_configured_login_state_detects_commufa_login(self) -> None:
        summary = {
            "url": "https://mypage.commufa.jp/join/s/",
            "title": "Myコミュファログイン",
            "text": "ログインID パスワード",
            "passwordFields": 1,
        }
        self.assertEqual(
            classify_configured_login_state(summary, SERVICE_AUTOMATION_CONFIGS["commufa"]),
            "login-required",
        )

    def test_commufa_usage_month_asserts_target(self) -> None:
        content = "ご利用年月 2026年1月分 請求金額 5,000円".encode("utf-8")
        assert_commufa_usage_month(content, "2026-01")
        with self.assertRaises(Exception):
            assert_commufa_usage_month(content, "2026-02")


if __name__ == "__main__":
    unittest.main()
