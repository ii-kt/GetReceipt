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

from src.storage.drive_storage import DriveStorage
from src.config import service_by_id


class FakeDriveStorage:
    def __init__(self, files: list[dict[str, str]]):
        self.files = files

    def list_files(self) -> list[dict[str, str]]:
        return list(self.files)


def receipt_file(date_key: str, partner: str, amount: int) -> dict[str, str]:
    name = f"{date_key}_{partner}_{amount}円.pdf"
    return {
        "id": name,
        "name": name,
        "mimeType": "application/pdf",
        "size": "1024",
        "webViewLink": f"https://drive.google.com/{name}",
    }


class StreamlitAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError as error:  # pragma: no cover - deployment dependency guard
            raise unittest.SkipTest("streamlit testing is unavailable") from error
        cls.AppTest = AppTest

    def app(self):
        app = self.AppTest.from_file(str(CLOUD / "streamlit_app.py"))
        app.secrets = {
            "google_service_account": {"project_id": "test"},
            "epos": {"login_id": "test", "password": "test"},
            "commufa": {"login_id": "test", "password": "test"},
            "tokuten": {"email": "test", "password": "test"},
            "webbilling": {"d_account_id": "test", "password": "test"},
        }
        return app

    def test_missing_drive_configuration_stops_without_legacy_ui(self) -> None:
        app = self.AppTest.from_file(str(CLOUD / "streamlit_app.py")).run(timeout=20)

        self.assertEqual([], list(app.exception))
        self.assertEqual(1, len(app.selectbox))
        self.assertEqual(0, len(app.tabs))
        self.assertEqual(0, len(app.file_uploader))

    def test_drive_files_are_the_only_saved_status_source(self) -> None:
        files = [
            receipt_file("20260705", "株式会社エポスカード", 87560),
            receipt_file("20260711", "中部テレコミュニケーション株式会社", 6710),
            receipt_file("20260712", "フラットエナジー株式会社", 7515),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("未取得は1件です", markdown)
        self.assertIn("NTTファイナンス株式会社", markdown)
        self.assertTrue(any("未取得1件" in button.label for button in app.button))

    def test_all_four_drive_files_remove_acquisition_button(self) -> None:
        files = [
            receipt_file("20260705", "株式会社エポスカード", 87560),
            receipt_file("20260711", "中部テレコミュニケーション株式会社", 6710),
            receipt_file("20260712", "フラットエナジー株式会社", 7515),
            receipt_file("20260709", "NTTファイナンス株式会社", 4882),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_single_button_acquires_each_missing_service_and_finishes(self) -> None:
        files = [
            receipt_file("20260705", "株式会社エポスカード", 87560),
            receipt_file("20260711", "中部テレコミュニケーション株式会社", 6710),
        ]
        storage = FakeDriveStorage(files)
        acquired: list[str] = []

        def acquire(**kwargs):
            service_id = kwargs["service_id"]
            target_month = kwargs["target_month"]
            acquired.append(service_id)
            file = receipt_file(
                f"{target_month.replace('-', '')}01",
                service_by_id(service_id).default_partner,
                5000 + len(acquired),
            )
            storage.files.append(file)
            return SimpleNamespace(success=True, failure=None, file_name=file["name"])

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
        ):
            app.run(timeout=20)
            acquisition_button = next(button for button in app.button if "自動取得" in button.label)
            acquisition_button.click().run(timeout=20)

        self.assertEqual(acquired, ["tokuten", "mobile"])
        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_first_failure_ends_batch_without_running_following_services(self) -> None:
        storage = FakeDriveStorage([])
        attempted: list[str] = []

        def fail(**kwargs):
            attempted.append(kwargs["service_id"])
            return SimpleNamespace(
                success=False,
                file_name="",
                failure=SimpleNamespace(code="LOGIN_FAILED", message="ログインに失敗しました。"),
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=fail),
        ):
            app.run(timeout=20)
            acquisition_button = next(button for button in app.button if "自動取得" in button.label)
            acquisition_button.click().run(timeout=20)

        self.assertEqual(attempted, ["epos"])
        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("自動取得に失敗したため終了しました", markdown)
        self.assertTrue(any("再度自動取得" in button.label for button in app.button))


if __name__ == "__main__":
    unittest.main()
