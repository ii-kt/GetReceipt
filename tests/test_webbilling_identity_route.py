"""Which identity the owner registered decides which sign-in runs.

Web billing accepts two, and they are nothing alike. A d-account goes out to
docomo, which fronts it with an image check only a person can answer. NTT
Finance's own ID stays on their site and mails a one-time password to the
mailbox this app already reads - nothing to answer, nothing to tap.

The decision was read off whichever key happened to hold the login id, and
that key was filled in from any of them, so every configuration reported a
d-account identity and the Web billing route could not be reached at all.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.credentials import service_credentials  # noqa: E402
from src.automation.official_sites import (  # noqa: E402
    build_webbilling_auto_login_expression,
)


def _payload(credentials: dict) -> dict:
    """The values the login script is actually handed."""

    expression = build_webbilling_auto_login_expression(credentials)
    match = re.search(r"const payload = (\{.*?\});\n", expression, re.S)
    assert match is not None, "payload not found in the generated script"
    return json.loads(match.group(1))


class IdentityRouteTest(unittest.TestCase):
    def test_a_web_billing_id_takes_the_portal_route(self) -> None:
        credentials = service_credentials(
            {"webbilling": {"webbilling_id": "WB12345678", "password": "secret-value"}},
            "mobile",
        )

        self.assertEqual("WB12345678", credentials["login_id"])
        self.assertEqual("", credentials["dAccountId"])
        self.assertFalse(_payload(credentials)["prefersDAccount"])

    def test_a_d_account_still_takes_the_d_account_route(self) -> None:
        credentials = service_credentials(
            {"webbilling": {"d_account_id": "someone@example.com", "password": "secret-value"}},
            "mobile",
        )

        self.assertEqual("someone@example.com", credentials["dAccountId"])
        self.assertTrue(_payload(credentials)["prefersDAccount"])

    def test_a_web_billing_id_wins_over_a_d_account_left_beside_it(self) -> None:
        """Naming it is how the owner asks for the route that needs nothing."""

        credentials = service_credentials(
            {
                "webbilling": {
                    "d_account_id": "someone@example.com",
                    "webbilling_id": "WB12345678",
                    "password": "secret-value",
                }
            },
            "mobile",
        )

        self.assertEqual("WB12345678", credentials["login_id"])
        self.assertFalse(_payload(credentials)["prefersDAccount"])

    def test_an_unnamed_email_is_still_read_as_a_d_account(self) -> None:
        """Which is what the existing setup relies on."""

        credentials = service_credentials(
            {"webbilling": {"login_id": "someone@example.com", "password": "secret-value"}},
            "mobile",
        )

        self.assertTrue(_payload(credentials)["prefersDAccount"])

    def test_a_plain_id_no_longer_reports_a_d_account_it_never_had(self) -> None:
        credentials = service_credentials(
            {"webbilling": {"login_id": "WB12345678", "password": "secret-value"}},
            "mobile",
        )

        self.assertEqual("", credentials["dAccountId"])
        self.assertFalse(_payload(credentials)["prefersDAccount"])

    def test_the_password_still_reaches_the_script(self) -> None:
        credentials = service_credentials(
            {"webbilling": {"webbilling_id": "WB12345678", "password": "secret-value"}},
            "mobile",
        )
        payload = _payload(credentials)

        self.assertEqual("WB12345678", payload["dAccountId"])
        self.assertEqual("secret-value", payload["password"])

    def test_other_services_are_untouched(self) -> None:
        credentials = service_credentials(
            {"commufa": {"login_id": "someone@example.com", "password": "secret-value"}},
            "commufa",
        )

        self.assertEqual("someone@example.com", credentials["login_id"])
        self.assertEqual("someone@example.com", credentials["email"])


if __name__ == "__main__":
    unittest.main()
