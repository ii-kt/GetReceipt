from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.oauth.microsoft import (  # noqa: E402
    MicrosoftOAuthConfig,
    MicrosoftOAuthError,
    MicrosoftOAuthManager,
    MicrosoftTokenStore,
)


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, refresh_error: str = "", refresh_status: int = 400) -> None:
        self.forms: list[dict[str, str]] = []
        self.refresh_error = refresh_error
        self.refresh_status = refresh_status

    def post(self, _url, *, data, headers, timeout):
        self.forms.append(dict(data))
        if data["grant_type"] == "authorization_code":
            return FakeResponse(
                {
                    "access_token": "access-token-initial-value",
                    "refresh_token": "refresh-token-initial-value",
                }
            )
        if self.refresh_error:
            return FakeResponse(
                {"error": self.refresh_error, "error_description": "sensitive detail"},
                status_code=self.refresh_status,
            )
        return FakeResponse(
            {
                "access_token": "access-token-refreshed-value",
                "refresh_token": "refresh-token-rotated-value",
            }
        )


class MicrosoftOAuthTest(unittest.TestCase):
    def test_pkce_callback_and_encrypted_refresh_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            key = Fernet.generate_key().decode("ascii")
            config = MicrosoftOAuthConfig(
                client_id="11111111-1111-1111-1111-111111111111",
                client_secret="confidential-client-secret-value",
                redirect_uri="https://get-receipt.streamlit.app/",
                encryption_key=key,
            )
            token_store = MicrosoftTokenStore(
                database_path=database,
                owner_id="owner-1",
                encryption_key=key,
            )
            session = FakeSession()
            manager = MicrosoftOAuthManager(
                config=config,
                token_store=token_store,
                session=session,
            )

            started = manager.start()
            parsed = urlsplit(started["authorization_url"])
            query = parse_qs(parsed.query)
            self.assertEqual("S256", query["code_challenge_method"][0])
            self.assertNotIn("code_verifier", query)
            self.assertIn("https://graph.microsoft.com/Mail.Read", query["scope"][0])

            # The callback can land after a worker restart. Pending PKCE state is
            # encrypted in SQLite instead of relying on process memory.
            restarted_manager = MicrosoftOAuthManager(
                config=config,
                token_store=MicrosoftTokenStore(
                    database_path=database,
                    owner_id="owner-1",
                    encryption_key=key,
                ),
                session=session,
            )
            completed = restarted_manager.complete(
                code="c" * 40,
                state=query["state"][0],
            )
            self.assertTrue(completed["connected"])
            raw_database = database.read_bytes()
            self.assertNotIn(b"refresh-token-initial-value", raw_database)
            self.assertNotIn(query["state"][0].encode("ascii"), raw_database)

            access_token = restarted_manager.access_token()
            self.assertEqual("access-token-refreshed-value", access_token)
            self.assertEqual(
                "refresh-token-rotated-value",
                token_store.load_refresh_token(),
            )
            self.assertEqual(
                ["authorization_code", "refresh_token"],
                [form["grant_type"] for form in session.forms],
            )

            with self.assertRaises(MicrosoftOAuthError) as replay:
                restarted_manager.complete(
                    code="c" * 40,
                    state=query["state"][0],
                )
            self.assertEqual("MICROSOFT_OAUTH_STATE_EXPIRED", replay.exception.code)

    def test_invalid_grant_requires_reconnect_and_deletes_stale_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            key = Fernet.generate_key().decode("ascii")
            token_store = MicrosoftTokenStore(
                database_path=database,
                owner_id="owner-1",
                encryption_key=key,
            )
            token_store.save_refresh_token("refresh-token-initial-value")
            manager = MicrosoftOAuthManager(
                config=_config(key),
                token_store=token_store,
                session=FakeSession(refresh_error="invalid_grant"),
            )

            with self.assertRaises(MicrosoftOAuthError) as raised:
                manager.access_token()

            self.assertEqual(
                "MICROSOFT_OAUTH_RECONNECT_REQUIRED",
                raised.exception.code,
            )
            self.assertFalse(manager.status()["connected"])
            self.assertEqual("", manager.status()["updated_at"])

    def test_transient_token_failure_keeps_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            key = Fernet.generate_key().decode("ascii")
            token_store = MicrosoftTokenStore(
                database_path=database,
                owner_id="owner-1",
                encryption_key=key,
            )
            token_store.save_refresh_token("refresh-token-initial-value")
            manager = MicrosoftOAuthManager(
                config=_config(key),
                token_store=token_store,
                session=FakeSession(refresh_error="server_error", refresh_status=500),
            )

            with self.assertRaises(MicrosoftOAuthError) as raised:
                manager.access_token()

            self.assertEqual("MICROSOFT_OAUTH_REJECTED", raised.exception.code)
            self.assertTrue(manager.status()["connected"])

    def test_wrong_encryption_key_fails_closed_and_removes_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            first_key = Fernet.generate_key().decode("ascii")
            first_store = MicrosoftTokenStore(
                database_path=database,
                owner_id="owner-1",
                encryption_key=first_key,
            )
            first_store.save_refresh_token("refresh-token-initial-value")

            replacement_store = MicrosoftTokenStore(
                database_path=database,
                owner_id="owner-1",
                encryption_key=Fernet.generate_key().decode("ascii"),
            )

            self.assertFalse(replacement_store.connected())
            self.assertEqual("", replacement_store.updated_at())


def _config(key: str) -> MicrosoftOAuthConfig:
    return MicrosoftOAuthConfig(
        client_id="11111111-1111-1111-1111-111111111111",
        client_secret="confidential-client-secret-value",
        redirect_uri="https://get-receipt.streamlit.app/",
        encryption_key=key,
    )


if __name__ == "__main__":
    unittest.main()


class MicrosoftAuthorizationCodeShapeTest(unittest.TestCase):
    """A real Microsoft authorization code is opaque and contains symbols."""

    def test_realistic_code_with_symbols_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            key = Fernet.generate_key().decode("ascii")
            config = MicrosoftOAuthConfig(
                client_id="11111111-1111-1111-1111-111111111111",
                client_secret="confidential-client-secret-value",
                redirect_uri="https://get-receipt.streamlit.app/",
                encryption_key=key,
            )
            token_store = MicrosoftTokenStore(
                database_path=Path(temp) / "jobs.sqlite3",
                owner_id="owner-1",
                encryption_key=key,
            )
            session = FakeSession()
            manager = MicrosoftOAuthManager(
                config=config,
                token_store=token_store,
                session=session,
            )
            state = parse_qs(urlsplit(manager.start()["authorization_url"]).query)[
                "state"
            ][0]

            # Shape taken from a live Entra callback: dots, underscores,
            # hyphens, "!" and "*" all appear in the code.
            realistic_code = (
                "M.C512_BAY.2.U.-Ah!Q1lIzYyZ*abcDEF0123456789"
                "gHiJkLmNoPqRsTuVwXyZ_-.~abcdefghijklmnop"
            )
            completed = manager.complete(code=realistic_code, state=state)

            self.assertTrue(completed["connected"])
            self.assertEqual(realistic_code, session.forms[0]["code"])

    def test_control_characters_are_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            key = Fernet.generate_key().decode("ascii")
            manager = MicrosoftOAuthManager(
                config=MicrosoftOAuthConfig(
                    client_id="11111111-1111-1111-1111-111111111111",
                    client_secret="confidential-client-secret-value",
                    redirect_uri="https://get-receipt.streamlit.app/",
                    encryption_key=key,
                ),
                token_store=MicrosoftTokenStore(
                    database_path=Path(temp) / "jobs.sqlite3",
                    owner_id="owner-1",
                    encryption_key=key,
                ),
                session=FakeSession(),
            )
            state = parse_qs(urlsplit(manager.start()["authorization_url"]).query)[
                "state"
            ][0]

            with self.assertRaises(MicrosoftOAuthError) as rejected:
                manager.complete(code="bad\ncode\rinjection" + "x" * 20, state=state)
            self.assertEqual(
                "MICROSOFT_OAUTH_RESPONSE_INVALID", rejected.exception.code
            )
