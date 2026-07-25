from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.storage.drive_storage import (  # noqa: E402
    DriveStorage,
    load_user_oauth_config,
)


_OAUTH_SECRETS = {
    "google_oauth": {
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "refresh_token": "1//refresh-token",
    },
    "google_service_account": {"project_id": "p", "private_key": "k"},
}


class DriveCredentialChoiceTest(unittest.TestCase):
    def test_owner_oauth_is_preferred_over_service_account(self) -> None:
        """A service account has no quota, so owner OAuth must win."""

        with (
            patch(
                "src.storage.drive_storage.build_user_drive_service",
                return_value="user-service",
            ) as user_builder,
            patch("src.storage.drive_storage.build_drive_service") as sa_builder,
        ):
            storage = DriveStorage.from_secrets(_OAUTH_SECRETS)

        self.assertEqual("user-service", storage.service)
        user_builder.assert_called_once()
        sa_builder.assert_not_called()

    def test_service_account_is_used_when_no_oauth_configured(self) -> None:
        secrets = {"google_service_account": {"project_id": "p", "private_key": "k"}}
        with (
            patch(
                "src.storage.drive_storage.build_drive_service",
                return_value="sa-service",
            ) as sa_builder,
            patch(
                "src.storage.drive_storage.load_service_account_info",
                return_value={"project_id": "p"},
            ),
        ):
            storage = DriveStorage.from_secrets(secrets)

        self.assertEqual("sa-service", storage.service)
        sa_builder.assert_called_once()

    def test_incomplete_oauth_section_is_ignored(self) -> None:
        secrets = {
            "google_oauth": {"client_id": "only-id"},
        }
        self.assertIsNone(load_user_oauth_config(secrets))

    def test_oauth_config_reads_all_three_values(self) -> None:
        config = load_user_oauth_config(_OAUTH_SECRETS)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual("1//refresh-token", config["refresh_token"])


if __name__ == "__main__":
    unittest.main()
