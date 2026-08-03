"""Drive the real step script with a real browser, against real page shapes.

The selector logic lives in JavaScript, so nothing that stubs the browser can
tell whether it picks the right control. These pages are rebuilt from what the
acquisition itself reported, and Chrome runs the actual expression over them.

Skipped where no browser is installed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

os.environ.setdefault("GETRECEIPT_ALLOW_CHROMIUM", "1")

from src.automation.browser_session import (  # noqa: E402
    ManagedBrowser,
    find_browser_executable,
)
from src.automation.official_sites import build_commufa_step_expression  # noqa: E402


_HEAD = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>Myコミュファ</title></head><body>
<header><span>Myコミュファ</span>
  <nav>
    <a href="/join/s/">ホーム</a>
    <a href="/join/s/mypage">マイページ</a>
    <a href="/join/s/family-invite">家族招待</a>
    <a href="/join/s/family">家族情報</a>
    <a href="/join/s/settings">ログイン情報の変更</a>
    <a href="/join/s/logout">ログアウト</a>
  </nav>
</header><main id="main">"""
_TAIL = "</main></body></html>"

# The page the failing acquisition reported: signed in, greeting the member by
# name, offering the billing screen behind a plain 詳しくはこちら.
LANDING = _HEAD + """
  <p>飯野 海斗</p>
  <section><div><h2>2026年6月のご利用料金</h2><p>6,710 円</p>
    <a href="/join/s/billing" id="entry">詳しくはこちら</a></div></section>
  <p>※iPhone等をご利用の方でポップアップブロックが有効になっている場合に、
  コミュポンのクリックができない場合があります。<a href="/help">詳細はこちら</a></p>
  <section><div><h3>Netflixパック</h3><a href="/ad/netflix">詳しくはこちら</a></div>
    <div><h3>Huluパック</h3><a href="/ad/hulu">詳しくはこちら</a></div></section>
""" + _TAIL

# The same page with the real entry gone. Only advert cards use the wording,
# and none of them may be pressed.
ADVERTS_ONLY = _HEAD + """
  <p>飯野 海斗</p>
  <section><div><h3>Netflixパック</h3><a href="/ad/netflix">詳しくはこちら</a></div>
    <div><h3>Huluパック</h3><a href="/ad/hulu">詳しくはこちら</a></div></section>
""" + _TAIL

BILLING_TOP = _HEAD + """
  <h1>ご利用料金・契約内容のご確認</h1>
  <section><div><a href="/join/s/CW40004">過去の請求額の一覧</a></div></section>
""" + _TAIL


@unittest.skipUnless(find_browser_executable(), "no browser installed")
class CommufaStepNavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        cls.browser = ManagedBrowser(
            profile_dir=root / "profile", download_dir=root / "downloads"
        )
        cls.root = root

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close(clear_profile=True)
        finally:
            cls._temp.cleanup()

    def _step(self, name: str, html: str) -> dict:
        page = self.root / f"{name}.html"
        page.write_text(html, encoding="utf-8")
        self.browser.navigate(page.as_uri(), wait_seconds=1.0)
        return self.browser.evaluate(build_commufa_step_expression(2026, 6), timeout=30) or {}

    def test_the_billing_entry_is_found_on_the_members_landing_page(self) -> None:
        """The header's ログアウト used to cancel this link out.

        Exclusion words were weighed against the text around a control, and
        the climb reached <body> - so the whole page counted as "around" it.
        """

        action = self._step("landing", LANDING)

        self.assertEqual("CLICK_BILLING_ENTRY", action.get("code"))
        self.assertIn("/join/s/billing", (action.get("logs") or [""])[0])

    def test_an_advert_offering_the_same_words_is_still_refused(self) -> None:
        """Which is why the surroundings are bounded rather than ignored.

        The advert's link says only 詳しくはこちら; what marks it as an advert
        is the card around it.
        """

        action = self._step("adverts", ADVERTS_ONLY)

        self.assertEqual("CONTRACT_BILLING_PAGE_NOT_FOUND", action.get("code"))
        self.assertIsNone(action.get("click"))

    def test_the_past_bill_list_is_found_from_the_billing_top(self) -> None:
        action = self._step("billing-top", BILLING_TOP)

        self.assertEqual("CLICK_PAST_BILL_LIST", action.get("code"))
        self.assertIn("CW40004", (action.get("logs") or [""])[0])


if __name__ == "__main__":
    unittest.main()
