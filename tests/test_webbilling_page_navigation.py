"""Drive the Web billing step script with a real browser.

The selector logic is JavaScript, so a stubbed browser proves nothing about
which control it picks. These pages mirror the certificate journey the 携帯
acquisition walks.

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
from src.automation.official_sites import (  # noqa: E402
    build_webbilling_step_expression,
)


_HEAD = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>Webビリング</title></head><body>
<header><nav>
  <a href="/mem/menu">請求内容のご確認</a>
  <a href="/mem/cert">料金支払証明書・ご利用料金証明書</a>
  <a href="/mem/invoice">適格請求書</a>
  <a href="/mem/logout">ログアウト</a>
</nav></header><main>"""
_TAIL = "</main></body></html>"

CERTIFICATE_LIST = _HEAD + """
  <h1>対象選択</h1>
  <table><thead><tr><th></th><th>請求年月</th><th>支払年月日</th><th>お支払/ご利用金額</th></tr></thead>
  <tbody>
    <tr><td><label><input type="checkbox" name="r1"></label></td>
        <td>2026年7月分</td><td>2026年7月9日</td><td>3,883円</td></tr>
    <tr><td><label><input type="checkbox" name="r2"></label></td>
        <td>2026年6月分</td><td>2026年6月9日</td><td>4,882円</td></tr>
  </tbody></table>
  <div><button type="button" disabled>次へ</button></div>
""" + _TAIL

CERTIFICATE_LIST_WITHOUT_TARGET = _HEAD + """
  <h1>対象選択</h1>
  <table><thead><tr><th></th><th>請求年月</th><th>支払年月日</th><th>お支払/ご利用金額</th></tr></thead>
  <tbody>
    <tr><td><label><input type="checkbox" name="r1"></label></td>
        <td>2026年6月分</td><td>2026年6月9日</td><td>4,882円</td></tr>
    <tr><td><label><input type="checkbox" name="r2"></label></td>
        <td>2026年5月分</td><td>2026年5月9日</td><td>3,735円</td></tr>
  </tbody></table>
""" + _TAIL

CONSENT_PAGE = _HEAD + """
  <h1>証明書のダウンロード</h1>
  <section><h2>注意事項</h2>
    <p>本証明書は再発行できません。</p>
    <label><input type="checkbox" name="agree"><span class="checkbox-parts"></span>
      上記の注意事項に同意します</label>
  </section>
  <section><h2>メールでのお知らせ</h2>
    <label><input type="checkbox" name="news">お知らせメールを受け取る</label>
  </section>
  <div><a id="btnDl" class="btn-item-download" href="#modal">ダウンロード</a></div>
""" + _TAIL


@unittest.skipUnless(find_browser_executable(), "no browser installed")
class WebBillingStepNavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls.browser = ManagedBrowser(
            profile_dir=cls.root / "profile", download_dir=cls.root / "downloads"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close(clear_profile=True)
        finally:
            cls._temp.cleanup()

    def _step(self, name: str, html: str, *, year: int = 2026, month: int = 7) -> dict:
        page = self.root / f"{name}.html"
        page.write_text(html, encoding="utf-8")
        self.browser.navigate(page.as_uri(), wait_seconds=1.0)
        return self.browser.evaluate(
            build_webbilling_step_expression(year, month), timeout=30
        ) or {}

    def test_the_requested_month_is_the_row_that_gets_ticked(self) -> None:
        action = self._step("cert-list", CERTIFICATE_LIST)

        self.assertEqual("CLICK_TARGET_CHECKBOX", action.get("code"))
        log = (action.get("logs") or [""])[0]
        self.assertIn("2026年7月分", log)
        self.assertNotIn("2026年6月分", log)

    def test_a_month_not_listed_comes_back_with_what_was_listed(self) -> None:
        """That list is what turns this into 未発行 rather than a failure."""

        action = self._step("cert-list-missing", CERTIFICATE_LIST_WITHOUT_TARGET)

        self.assertEqual("YEAR_MONTH_NOT_AVAILABLE", action.get("code"))
        self.assertIn("2026/06", action.get("availableMonths") or [])

    def test_only_the_notice_checkbox_is_agreed_to(self) -> None:
        """Eight levels of surroundings reached <body>, so every checkbox on
        the page looked like the one carrying 同意."""

        action = self._step("consent", CONSENT_PAGE)

        self.assertEqual("CLICK_CONSENT", action.get("code"))
        ticked = self.browser.evaluate(
            "(() => ({agree: document.querySelector(\"input[name='agree']\").checked,"
            " news: document.querySelector(\"input[name='news']\").checked}))()",
            timeout=10,
        )
        # The step reports where to click rather than ticking it itself, so
        # what matters is that it named the consent box and not the other one.
        self.assertIn("同意", (action.get("logs") or [""])[0])
        self.assertFalse(ticked["news"])

    def test_the_download_follows_once_consent_is_given(self) -> None:
        page = self.root / "consent-given.html"
        page.write_text(
            CONSENT_PAGE.replace(
                '<input type="checkbox" name="agree">',
                '<input type="checkbox" name="agree" checked>',
            ),
            encoding="utf-8",
        )
        self.browser.navigate(page.as_uri(), wait_seconds=1.0)
        action = self.browser.evaluate(
            build_webbilling_step_expression(2026, 7), timeout=30
        ) or {}

        self.assertEqual("CLICK_DOWNLOAD", action.get("code"))
        self.assertTrue(action.get("mayDownload"))


if __name__ == "__main__":
    unittest.main()
