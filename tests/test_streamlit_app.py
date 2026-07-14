from __future__ import annotations

import importlib
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
from src.config import expected_transaction_month, service_by_id
from src.ui import styles as ui_styles_module


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


def drive_file(
    name: str,
    *,
    file_id: str | None = None,
    mime_type: str = "application/pdf",
    size: str | None = "1024",
) -> dict[str, str]:
    file = {
        "id": file_id or name,
        "name": name,
        "mimeType": mime_type,
        "webViewLink": f"https://drive.google.com/{file_id or name}",
    }
    if size is not None:
        file["size"] = size
    return file


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
        app.session_state["getreceipt_month"] = "2026-07"
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
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260811", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
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
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260811", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_archive_mode_keeps_monthly_completion_and_lists_only_one_off_receipts(self) -> None:
        files = [
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260811", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
            drive_file("20260530_Anthropic_$10-$10.pdf", file_id="anthropic-refund"),
            drive_file("20260530_OpenAI_1000円-1000円.pdf", file_id="openai-refund"),
            drive_file("20260708_交通サービス_500円.pdf", file_id="transport"),
            drive_file(
                "電子取引データに関する事務処理規程",
                file_id="policy-document",
                mime_type="application/vnd.google-apps.document",
                size=None,
            ),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)

            monthly_markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", monthly_markdown)
            self.assertFalse(any("自動取得" in button.label for button in app.button))

            app.segmented_control[0].set_value("単発領収書").run(timeout=20)

        self.assertEqual([], list(app.exception))
        archive_markdown = "\n".join(item.value for item in app.markdown)
        self.assertEqual(
            1,
            archive_markdown.count("<strong>3</strong><span>件の単発領収書</span>"),
        )
        self.assertIn("$10-$10", archive_markdown)
        self.assertIn("1000円-1000円", archive_markdown)
        self.assertIn("交通サービス", archive_markdown)
        self.assertNotIn("株式会社エポスカード", archive_markdown)
        self.assertNotIn("中部テレコミュニケーション株式会社", archive_markdown)
        self.assertNotIn("フラットエナジー株式会社", archive_markdown)
        self.assertNotIn("NTTファイナンス株式会社", archive_markdown)
        self.assertNotIn("電子取引データに関する事務処理規程", archive_markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_stale_cached_ui_module_is_reloaded_before_archive_render(self) -> None:
        files = [
            drive_file("20260708_交通サービス_500円.pdf", file_id="transport"),
        ]
        app = self.app()
        ui_styles_module.UI_API_VERSION = 1
        delattr(ui_styles_module, "render_archive_hero")
        try:
            with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
                app.run(timeout=20)
                app.segmented_control[0].set_value("単発領収書").run(timeout=20)
        finally:
            importlib.reload(ui_styles_module)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("交通サービス", markdown)

    def test_archive_filters_month_currency_refund_and_search_together(self) -> None:
        files = [
            drive_file("20260530_Anthropic_$10-$10.pdf", file_id="anthropic-refund"),
            drive_file("20260530_OpenAI_1000円-1000円.pdf", file_id="openai-refund"),
            drive_file("20260615_OpenAI_$8.pdf", file_id="openai-june"),
            drive_file("20260708_交通サービス_500円.pdf", file_id="transport"),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)
            app.segmented_control[0].set_value("単発領収書").run(timeout=20)

            app.selectbox[0].set_value("2026-05").run(timeout=20)
            markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("Anthropic", markdown)
            self.assertIn("OpenAI", markdown)
            self.assertNotIn("交通サービス", markdown)

            app.selectbox[1].set_value("USD").run(timeout=20)
            markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("Anthropic", markdown)
            self.assertNotIn("1000円-1000円", markdown)

            app.selectbox[2].set_value("refund").run(timeout=20)
            app.text_input[0].set_value("anthropic").run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("<strong>1</strong><span>/ 4 件を表示</span>", markdown)
        self.assertIn("20260530_Anthropic_$10-$10.pdf", markdown)
        self.assertNotIn("20260530_OpenAI_1000円-1000円.pdf", markdown)
        self.assertNotIn("20260615_OpenAI_$8.pdf", markdown)
        self.assertNotIn("20260708_交通サービス_500円.pdf", markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_many_one_off_receipts_do_not_fill_a_missing_monthly_service(self) -> None:
        files = [
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260811", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            drive_file("20260701_OpenAI_$8.pdf", file_id="one-off-1"),
            drive_file("20260702_Anthropic_$12.pdf", file_id="one-off-2"),
            drive_file("20260703_小売サービス_1200円.pdf", file_id="one-off-3"),
            drive_file("20260704_通販サービス_4500円.pdf", file_id="one-off-4"),
            drive_file("20260705_会計サービス_2980円.pdf", file_id="one-off-5"),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)

            monthly_markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("未取得は1件です", monthly_markdown)
            self.assertIn("NTTファイナンス株式会社", monthly_markdown)
            self.assertTrue(any("未取得1件" in button.label for button in app.button))

            app.segmented_control[0].set_value("単発領収書").run(timeout=20)

        self.assertEqual([], list(app.exception))
        archive_markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("<strong>5</strong><span>件の単発領収書</span>", archive_markdown)
        self.assertFalse(any("自動取得" in button.label for button in app.button))

    def test_single_button_acquires_each_missing_service_and_finishes(self) -> None:
        files = [
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260811", "中部テレコミュニケーション株式会社", 10002),
        ]
        storage = FakeDriveStorage(files)
        acquired: list[str] = []

        def acquire(**kwargs):
            service_id = kwargs["service_id"]
            target_month = kwargs["target_month"]
            acquired.append(service_id)
            transaction_month = expected_transaction_month(service_id, target_month)
            file = receipt_file(
                f"{transaction_month.replace('-', '')}01",
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
