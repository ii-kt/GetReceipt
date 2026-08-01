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

    def test_it_asks_only_for_drive(self) -> None:
        self.assertEqual([GOOGLE_DRIVE_SCOPE], self.query["scope"])


class ExtractCodeTest(unittest.TestCase):
    """On a phone the code only ever appears in the address bar."""

    def test_the_whole_redirected_address_is_accepted(self) -> None:
        code = extract_code(
            "http://localhost:1/?code=4%2F0AVMBsJgABC-def_123&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive"
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
            "http://localhost:1/?error=access_denied",
            "https://example.test/",
        ):
            with self.subTest(value=value):
                self.assertEqual("", extract_code(value))

    def test_a_value_that_is_not_a_code_is_refused(self) -> None:
        self.assertEqual("", extract_code("short"))
        self.assertEqual("", extract_code("x" * 5000))


if __name__ == "__main__":
    unittest.main()
