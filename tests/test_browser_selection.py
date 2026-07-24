from __future__ import annotations

import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.browser_session import (  # noqa: E402
    ManagedBrowser,
    find_browser_executable,
)


class BrowserSelectionTest(unittest.TestCase):
    def test_google_chrome_is_the_only_automatic_browser_family(self) -> None:
        queried: list[str] = []

        def which(name: str):
            queried.append(name)
            return "/usr/bin/google-chrome" if name == "google-chrome" else None

        with (
            patch.dict(
                os.environ,
                {
                    "BROWSER_EXECUTABLE": "",
                    "GETRECEIPT_ALLOW_CHROMIUM": "",
                    "CHROME_BIN": "",
                    "PROGRAMFILES": "",
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                },
                clear=False,
            ),
            patch("src.automation.browser_session.shutil.which", side_effect=which),
            patch(
                "src.automation.browser_session._path_exists",
                side_effect=lambda value: value if value == "/usr/bin/google-chrome" else None,
            ),
        ):
            selected = find_browser_executable()

        self.assertEqual("/usr/bin/google-chrome", selected)
        self.assertEqual(["google-chrome"], queried)
        self.assertNotIn("chromium", queried)
        self.assertNotIn("chromium-browser", queried)

    def test_explicit_google_chrome_path_wins(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "BROWSER_EXECUTABLE": "C:\\Chrome\\chrome.exe",
                    "GETRECEIPT_ALLOW_CHROMIUM": "",
                    "CHROME_BIN": "",
                },
                clear=False,
            ),
            patch(
                "src.automation.browser_session._path_exists",
                side_effect=lambda value: value if value == "C:\\Chrome\\chrome.exe" else None,
            ),
            patch("src.automation.browser_session.shutil.which") as which,
        ):
            selected = find_browser_executable()

        self.assertEqual("C:\\Chrome\\chrome.exe", selected)
        which.assert_not_called()

    def test_explicit_edge_or_chromium_path_is_rejected(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "BROWSER_EXECUTABLE": "C:\\Browser\\msedge.exe",
                    "GETRECEIPT_ALLOW_CHROMIUM": "",
                    "CHROME_BIN": "C:\\Browser\\chromium.exe",
                    "PROGRAMFILES": "",
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                },
                clear=False,
            ),
            patch(
                "src.automation.browser_session._path_exists",
                side_effect=lambda value: value
                if value in {
                    "C:\\Browser\\msedge.exe",
                    "C:\\Browser\\chromium.exe",
                }
                else None,
            ),
            patch(
                "src.automation.browser_session.shutil.which",
                return_value=None,
            ),
        ):
            selected = find_browser_executable()

        self.assertIsNone(selected)

    def test_chrome_process_does_not_inherit_worker_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            browser = ManagedBrowser(
                profile_dir=root / "profile",
                download_dir=root / "downloads",
            )
            browser.port = 19021
            process = MagicMock()
            with (
                patch.dict(
                    os.environ,
                    {
                        "GETRECEIPT_API_TOKEN": "must-not-reach-chrome",
                        "GETRECEIPT_PROVIDER_CREDENTIALS_JSON": "must-not-reach-chrome",
                        "GOOGLE_SERVICE_ACCOUNT_JSON": "must-not-reach-chrome",
                        "PATH": "safe-path",
                        "TEMP": str(root),
                    },
                    clear=False,
                ),
                patch(
                    "src.automation.browser_session.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                browser._launch_browser(
                    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                    "--headless=new",
                )

        child_env = popen.call_args.kwargs["env"]
        self.assertEqual("safe-path", child_env["PATH"])
        self.assertEqual(str(root), child_env["TEMP"])
        self.assertFalse(
            any(
                name.startswith(("GETRECEIPT_", "GOOGLE_"))
                for name in child_env
            )
        )
        self.assertNotIn(
            "must-not-reach-chrome",
            repr(popen.call_args),
        )


if __name__ == "__main__":
    unittest.main()


class ChromiumFallbackFlagTest(unittest.TestCase):
    def test_chromium_is_selected_only_with_explicit_flag(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/bin/chromium" if name == "chromium" else None

        with (
            patch.dict(
                os.environ,
                {
                    "BROWSER_EXECUTABLE": "",
                    "CHROME_BIN": "",
                    "GETRECEIPT_ALLOW_CHROMIUM": "1",
                    "PROGRAMFILES": "",
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                },
                clear=False,
            ),
            patch("src.automation.browser_session.shutil.which", side_effect=which),
            patch(
                "src.automation.browser_session._path_exists",
                side_effect=lambda value: (
                    value if value == "/usr/bin/chromium" else None
                ),
            ),
        ):
            selected = find_browser_executable()

        self.assertEqual("/usr/bin/chromium", selected)

    def test_chromium_stays_rejected_without_flag(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/bin/chromium" if name == "chromium" else None

        with (
            patch.dict(
                os.environ,
                {
                    "BROWSER_EXECUTABLE": "",
                    "CHROME_BIN": "",
                    "GETRECEIPT_ALLOW_CHROMIUM": "",
                    "PROGRAMFILES": "",
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                },
                clear=False,
            ),
            patch("src.automation.browser_session.shutil.which", side_effect=which),
            patch(
                "src.automation.browser_session._path_exists",
                side_effect=lambda value: (
                    value if value == "/usr/bin/chromium" else None
                ),
            ),
        ):
            selected = find_browser_executable()

        self.assertIsNone(selected)
