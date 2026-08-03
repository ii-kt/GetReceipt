"""Every generated page script must actually run in a browser.

These are built as raw JavaScript strings, so a typo in one is invisible to
Python and only shows up as a failed acquisition against the live provider.
Each is evaluated here on a blank page: it must return a value rather than
throw, and it must not be trapped by the page-level stop that keeps a
control's surroundings from growing into the whole document.

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

from src.automation import epos, official_sites  # noqa: E402
from src.automation.browser_session import (  # noqa: E402
    ManagedBrowser,
    find_browser_executable,
)


CREDENTIALS = {"login_id": "someone@example.invalid", "password": "not-a-real-password"}


def _expressions() -> dict[str, str]:
    config = official_sites.SERVICE_AUTOMATION_CONFIGS["tokuten"]
    return {
        "commufa_step": official_sites.build_commufa_step_expression(2026, 6),
        "commufa_login": official_sites.build_configured_auto_login_expression(CREDENTIALS),
        "tokuten_step": official_sites.build_tokuten_step_expression(2026, 6, config),
        "mailbox_ready": official_sites.build_mailbox_ready_expression(),
        "outlook_search": official_sites.build_outlook_search_expression("トクテン 2026年6月"),
        "microsoft_login": official_sites.build_microsoft_auto_login_expression(CREDENTIALS),
        "webbilling_login": official_sites.build_webbilling_auto_login_expression(
            {"d_account_id": "someone", "password": "not-a-real-password"}
        ),
        "webbilling_step": official_sites.build_webbilling_step_expression(2026, 6),
        "epos_puzzle_state": epos.build_epos_puzzle_state_expression(),
    }


@unittest.skipUnless(find_browser_executable(), "no browser installed")
class PageScriptSyntaxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        cls.browser = ManagedBrowser(
            profile_dir=root / "profile", download_dir=root / "downloads"
        )
        page = root / "blank.html"
        page.write_text(
            "<!doctype html><html><body><main><section><div>"
            "<a href='#x'>リンク</a></div></section></main></body></html>",
            encoding="utf-8",
        )
        cls.browser.navigate(page.as_uri(), wait_seconds=1.0)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close(clear_profile=True)
        finally:
            cls._temp.cleanup()

    def test_every_script_runs_without_throwing(self) -> None:
        for name, expression in _expressions().items():
            with self.subTest(script=name):
                result = self.browser.evaluate(expression, timeout=20)
                self.assertIsNotNone(result, f"{name} returned nothing")

    def test_the_context_stop_is_in_place_everywhere(self) -> None:
        """One script kept climbing to <body> and it broke Wi-Fi acquisition.

        The surroundings of a control became the whole document, so the
        ログアウト in the header cancelled out the link being looked for.
        """

        for name, expression in _expressions().items():
            if "contextOf" not in expression:
                continue
            with self.subTest(script=name):
                self.assertIn(
                    "PAGE_LEVEL",
                    expression,
                    f"{name} still lets a control's surroundings reach the page",
                )


if __name__ == "__main__":
    unittest.main()
