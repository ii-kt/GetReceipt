from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.ui.google_link import (  # noqa: E402
    GOOGLE_DRIVE_SCOPE,
    REDIRECT_URI,
    authorization_url,
    extract_code,
)


class AuthorizationUrlTest(unittest.TestCase):
    """The consent request has to come back with a long-lived credential."""

    def setUp(self) -> None:
        self.query = urllib.parse.parse_qs(
            urllib.parse.urlparse(authorization_url("client-123.apps.googleusercontent.com")).query
        )

    def test_it_asks_for_a_refresh_token(self) -> None:
        """Without both of these Google reuses the grant and returns none."""

        self.assertEqual(["offline"], self.query["access_type"])
        self.assertEqual(["consent"], self.query["prompt"])

    def test_it_redirects_somewhere_the_registered_client_accepts(self) -> None:
        """The client is a desktop one: localhost only, and no https at all."""

        self.assertEqual([REDIRECT_URI], self.query["redirect_uri"])
        self.assertTrue(REDIRECT_URI.startswith("http://localhost"))

    def test_the_redirect_port_is_one_a_browser_will_actually_try(self) -> None:
        """On a browser-blocked port the phone loads forever instead of failing.

        The whole flow depends on the phone reaching a connection error fast,
        with the code still in the address bar.
        """

        port = int(urllib.parse.urlparse(REDIRECT_URI).port or 80)
        # Chrome and Safari refuse this list outright.
        blocked = {
            1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
            69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115,
            117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512,
            513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587,
            601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045,
            5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
        }
        self.assertNotIn(port, blocked)

    def test_it_asks_only_for_drive(self) -> None:
        self.assertEqual([GOOGLE_DRIVE_SCOPE], self.query["scope"])


class ExtractCodeTest(unittest.TestCase):
    """On a phone the code only ever appears in the address bar."""

    def test_the_whole_redirected_address_is_accepted(self) -> None:
        code = extract_code(
            "http://localhost:8080/?code=4%2F0AVMBsJgABC-def_123&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive"
        )

        self.assertEqual("4/0AVMBsJgABC-def_123", code)

    def test_a_bare_code_is_accepted_too(self) -> None:
        self.assertEqual("4/0AVMBsJgABC-def_123", extract_code("4/0AVMBsJgABC-def_123"))

    def test_surrounding_whitespace_from_a_paste_is_ignored(self) -> None:
        self.assertEqual(
            "4/0AVMBsJgABC-def_123", extract_code("  4/0AVMBsJgABC-def_123\n")
        )

    def test_an_address_without_a_code_yields_nothing(self) -> None:
        for value in (
            "",
            "   ",
            "http://localhost:8080/?error=access_denied",
            "https://example.test/",
        ):
            with self.subTest(value=value):
                self.assertEqual("", extract_code(value))

    def test_a_value_that_is_not_a_code_is_refused(self) -> None:
        self.assertEqual("", extract_code("short"))
        self.assertEqual("", extract_code("x" * 5000))


if __name__ == "__main__":
    unittest.main()
