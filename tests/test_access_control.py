from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.ui.access_control import require_owner_access  # noqa: E402


class StopCalled(RuntimeError):
    pass


class FakeSt:
    def __init__(
        self,
        *,
        logged_in=False,
        email="",
        sub="",
        iss="https://issuer.example",
        email_verified=True,
    ) -> None:
        claims = {
            "email": email,
            "sub": sub,
            "iss": iss,
            "email_verified": email_verified,
        }
        self.user = SimpleNamespace(
            is_logged_in=logged_in,
            email=email,
            sub=sub,
            iss=iss,
            email_verified=email_verified,
            get=lambda name: claims.get(name, ""),
        )
        self.errors: list[str] = []
        self.buttons: list[dict] = []
        self.codes: list[str] = []
        self.login = lambda *_args: None
        self.logout = lambda: None

    def title(self, _value):
        return None

    def caption(self, _value):
        return None

    def code(self, value, **_kwargs):
        self.codes.append(str(value))

    def error(self, value, **_kwargs):
        self.errors.append(str(value))

    def button(self, label, **kwargs):
        self.buttons.append({"label": label, **kwargs})
        return False

    def stop(self):
        raise StopCalled()


class AccessControlTest(unittest.TestCase):
    def test_local_mode_without_worker_remains_available(self) -> None:
        st = FakeSt()
        require_owner_access(st, {})
        self.assertEqual([], st.errors)

    def test_remote_worker_without_access_config_fails_closed(self) -> None:
        st = FakeSt()
        with self.assertRaises(StopCalled):
            require_owner_access(
                st,
                {"receipt_worker": {"base_url": "https://worker.example"}},
            )
        self.assertIn("OWNER_AUTH_NOT_CONFIGURED", st.errors[0])

    def test_drive_credentials_without_access_config_also_fail_closed(self) -> None:
        st = FakeSt()
        with self.assertRaises(StopCalled):
            require_owner_access(
                st,
                {"google_service_account": {"client_email": "worker@example.com"}},
            )
        self.assertIn("OWNER_AUTH_NOT_CONFIGURED", st.errors[0])

    def test_provider_credentials_without_access_config_fail_closed(self) -> None:
        for section in ("epos", "commufa", "microsoft", "webbilling"):
            with self.subTest(section=section):
                st = FakeSt()
                with self.assertRaises(StopCalled):
                    require_owner_access(
                        st,
                        {section: {"login_id": "owner", "password": "secret"}},
                    )
                self.assertIn("OWNER_AUTH_NOT_CONFIGURED", st.errors[0])

    def test_oidc_login_is_required_before_owner_data(self) -> None:
        st = FakeSt(logged_in=False)
        with self.assertRaises(StopCalled):
            require_owner_access(
                st,
                {
                    "receipt_worker": {"base_url": "https://worker.example"},
                    "app_access": {
                        "mode": "oidc",
                        "allowed_emails": ["owner@example.com"],
                        "allowed_issuers": ["https://issuer.example"],
                    },
                },
            )
        self.assertEqual("所有者としてログイン", st.buttons[0]["label"])

    def test_allowlisted_email_passes_and_other_identity_stops(self) -> None:
        secrets = {
            "receipt_worker": {"base_url": "https://worker.example"},
            "app_access": {
                "mode": "oidc",
                "allowed_emails": ["OWNER@example.com"],
                "allowed_issuers": ["https://issuer.example"],
            },
        }
        require_owner_access(
            FakeSt(logged_in=True, email="owner@example.com", sub="subject-1"),
            secrets,
        )
        denied = FakeSt(logged_in=True, email="other@example.com", sub="subject-2")
        with self.assertRaises(StopCalled):
            require_owner_access(denied, secrets)
        self.assertIn("許可されていません", denied.errors[0])

    def test_unverified_email_is_not_accepted(self) -> None:
        secrets = {
            "app_access": {
                "mode": "oidc",
                "allowed_emails": ["owner@example.com"],
                "allowed_issuers": ["https://issuer.example"],
            },
        }
        denied = FakeSt(
            logged_in=True,
            email="owner@example.com",
            sub="subject-1",
            email_verified=False,
        )

        with self.assertRaises(StopCalled):
            require_owner_access(denied, secrets)

    def test_issuer_and_subject_identity_pair_is_accepted(self) -> None:
        secrets = {
            "app_access": {
                "mode": "oidc",
                "allowed_identities": [
                    "https://issuer.example|subject-1",
                ],
            },
        }

        require_owner_access(
            FakeSt(
                logged_in=True,
                email="",
                sub="subject-1",
                iss="https://issuer.example",
                email_verified=False,
            ),
            secrets,
        )

    def test_hashed_identity_supports_safe_first_login_enrollment(self) -> None:
        pending_secrets = {
            "app_access": {
                "mode": "oidc",
                "allowed_identity_hashes": ["sha256:pending-first-login"],
            },
        }
        first_login = FakeSt(
            logged_in=True,
            email="",
            sub="subject-1",
            iss="https://issuer.example",
            email_verified=False,
        )

        with self.assertRaises(StopCalled):
            require_owner_access(first_login, pending_secrets)

        self.assertEqual(1, len(first_login.codes))
        fingerprint = first_login.codes[0]
        self.assertTrue(fingerprint.startswith("sha256:"))
        self.assertEqual(71, len(fingerprint))

        require_owner_access(
            FakeSt(
                logged_in=True,
                email="",
                sub="subject-1",
                iss="https://issuer.example",
                email_verified=False,
            ),
            {
                "app_access": {
                    "mode": "oidc",
                    "allowed_identity_hashes": [fingerprint],
                },
            },
        )

    def test_environment_drive_credentials_fail_closed(self) -> None:
        st = FakeSt()
        with patch.dict(
            "os.environ",
            {"GOOGLE_SERVICE_ACCOUNT_JSON": '{"type":"service_account"}'},
            clear=False,
        ):
            with self.assertRaises(StopCalled):
                require_owner_access(st, {})

    def test_environment_provider_credentials_fail_closed(self) -> None:
        st = FakeSt()
        with patch.dict(
            "os.environ",
            {"GETRECEIPT_PROVIDER_CREDENTIALS_JSON": '{"epos":{}}'},
            clear=False,
        ):
            with self.assertRaises(StopCalled):
                require_owner_access(st, {})


if __name__ == "__main__":
    unittest.main()
