"""A bot check must reach the owner, not end the run as a login timeout.

The d-account sign-in is fronted by a check asking whether the visitor is a
person. It says none of the words the CAPTCHA detection looked for, in either
language, so it read as a page with no login controls: the sign-in wait ran to
its timeout and reported LOGIN_REQUIRED while the screen was asking a question
only a person can answer.

Nothing here answers it. Recognising it is what lets the app hold the browser
open and mirror the page, exactly as it already does for the Epos puzzle, so
the owner can answer it themselves.
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
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
    WebBillingAutoFetcher,
    build_webbilling_auto_login_expression,
)


CREDENTIALS = {"d_account_id": "someone", "password": "not-a-real-password"}

# Reproduced from what the failing acquisition reported on
# cfg.smt.docomo.ne.jp/auth/cgi/anidlogin.
HUMAN_CHECK = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>dアカウント</title></head><body><main>
  <h1>Let's confirm you are human</h1>
  <p>Complete the security check before continuing. This step verifies that
  you are not a bot, which helps to protect your account and prevent spam.</p>
  <div id="cf-chl-widget"></div>
</main></body></html>"""

# The same gate as an embedded widget rather than a full interstitial.
TURNSTILE_WIDGET = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>dアカウント</title></head><body><main>
  <h1>dアカウントのログイン</h1>
  <form><input type="text" name="id"><input type="password" name="pw">
  <div class="cf-turnstile"></div>
  <button type="submit">ログイン</button></form>
</main></body></html>"""

# An ordinary sign-in page must not be mistaken for one.
PLAIN_LOGIN = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>dアカウント</title></head><body><main>
  <h1>dアカウントのログイン</h1>
  <form><label>ID<input type="text" name="id"></label>
  <label>パスワード<input type="password" name="pw"></label>
  <button type="submit">ログイン</button></form>
</main></body></html>"""


@unittest.skipUnless(find_browser_executable(), "no browser installed")
class HumanCheckDetectionTest(unittest.TestCase):
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

    def _login_step(self, name: str, html: str) -> dict:
        page = self.root / f"{name}.html"
        page.write_text(html, encoding="utf-8")
        self.browser.navigate(page.as_uri(), wait_seconds=1.0)
        return self.browser.evaluate(
            build_webbilling_auto_login_expression(CREDENTIALS), timeout=20
        ) or {}

    def test_the_english_bot_check_is_named_rather_than_ignored(self) -> None:
        result = self._login_step("human-check", HUMAN_CHECK)

        self.assertEqual("SECURITY_CHALLENGE", result.get("code"))
        self.assertEqual("interactive", result.get("challengeKind"))
        # Never attempted: the password is not typed into a page that is
        # asking whether this is a person.
        self.assertFalse(result.get("attempted"))

    def test_the_embedded_widget_is_caught_too(self) -> None:
        result = self._login_step("turnstile", TURNSTILE_WIDGET)

        self.assertEqual("SECURITY_CHALLENGE", result.get("code"))
        self.assertEqual("interactive", result.get("challengeKind"))

    def test_an_ordinary_sign_in_page_is_left_alone(self) -> None:
        result = self._login_step("plain-login", PLAIN_LOGIN)

        self.assertNotEqual("SECURITY_CHALLENGE", result.get("code"))

    def test_the_one_time_code_page_is_not_taken_for_a_bot_check(self) -> None:
        """Misreading it as one would leave nowhere to type the code.

        The wrapper is deliberately id="challenge-form" - a Cloudflare
        interstitial uses that id, and so does an ordinary security-question
        form, which is why it is not one of the markers.
        """

        code_page = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
        <title>dアカウント</title></head><body><main>
          <h1>セキュリティコードの入力</h1>
          <form id="challenge-form">
            <p>SMSに送信したセキュリティコードを入力してください。</p>
            <input type="tel" name="otp" maxlength="6" aria-label="セキュリティコード">
            <button type="submit">次へ</button>
          </form>
        </main></body></html>"""

        result = self._login_step("code-page", code_page)

        # Off the provider's own https origin the script already refuses to
        # call anything a code prompt, so what this pins down is that the bot
        # check is not what claimed the page.
        self.assertNotIn("人間", str(result.get("reason") or ""))


class StalledSignInTest(unittest.TestCase):
    """Matching wording is not enough on its own.

    A check phrased some way nobody anticipated puts the run straight back to
    spending two minutes and reporting LOGIN_REQUIRED. What the page says is
    not dependable; that it is on the provider's own sign-in host, asking
    somebody something, and offering nothing this can drive - that is.
    """

    class _Browser:
        """Stuck on the d-account host with nothing the script can drive."""

        def __init__(self) -> None:
            self.evaluations = 0

        def page_summary(self) -> dict:
            return {
                "url": "https://cfg.smt.docomo.ne.jp/auth/cgi/anidlogin",
                "title": "dアカウント",
                "text": "Additional verification required before continuing.",
                "passwordFields": 0,
            }

        def evaluate(self, expression: str, **_kwargs):
            self.evaluations += 1
            return {
                "attempted": False,
                "code": "LOGIN_STEP_NOT_FOUND",
                "reason": "自動で進められるWebビリング/dアカウントのログイン操作を見つけられませんでした。",
            }

    def test_a_page_that_offers_nothing_goes_to_the_owner(self) -> None:
        import src.automation.official_sites as sites

        browser = self._Browser()
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]

        with unittest.mock.patch.object(sites, "_STALLED_LOGIN_SECONDS", 0.0):
            with unittest.mock.patch.object(sites.time, "sleep"):
                with self.assertRaises(Exception) as raised:
                    fetcher._wait_for_login(timeout_seconds=30)

        self.assertEqual("SECURITY_CHALLENGE", getattr(raised.exception, "code", ""))
        self.assertEqual(
            "interactive", str(getattr(raised.exception, "challenge_kind", ""))
        )
        # And it stopped early rather than sitting out the whole timeout.
        self.assertLessEqual(browser.evaluations, 3)

    def test_a_password_held_back_is_not_a_stall(self) -> None:
        """The one-submission guard also reports no progress.

        There the provider really is still working, and handing the page over
        would interrupt a sign-in that was going fine.
        """

        import src.automation.official_sites as sites

        class _Waiting(self._Browser):
            def evaluate(self, expression: str, **_kwargs):
                self.evaluations += 1
                return {
                    "attempted": True,
                    "code": "SUBMIT_PASSWORD",
                    "click": {"x": 1, "y": 1},
                }

        browser = _Waiting()
        fetcher = WebBillingAutoFetcher(browser)  # type: ignore[arg-type]
        browser.click_at = lambda x, y: None  # type: ignore[attr-defined]

        with unittest.mock.patch.object(sites, "_STALLED_LOGIN_SECONDS", 0.0):
            with unittest.mock.patch.object(sites.time, "sleep"):
                with self.assertRaises(Exception) as raised:
                    fetcher._wait_for_login(timeout_seconds=0.5)

        # Timed out waiting for the provider, not handed over as a gate.
        self.assertNotEqual("SECURITY_CHALLENGE", getattr(raised.exception, "code", ""))


class HumanCheckResumeTest(unittest.TestCase):
    def test_web_billing_can_pick_up_after_the_owner_clears_it(self) -> None:
        """Without this the app refuses the gate it just detected.

        It checks for a resume method before holding the browser open, and
        raised "this provider does not support safe resumption" instead.
        """

        self.assertTrue(
            callable(
                getattr(WebBillingAutoFetcher, "resume_after_interactive_challenge", None)
            )
        )

    def test_the_gate_is_not_called_a_puzzle(self) -> None:
        """There is no piece to drag on a bot check.

        Importing the app runs the page, so the wording helper is read from
        source rather than executed.
        """

        source = (CLOUD / "streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("def interactive_gate_wording", source)
        self.assertIn("確認チェック", source)


if __name__ == "__main__":
    unittest.main()
