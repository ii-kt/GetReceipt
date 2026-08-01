from __future__ import annotations

import importlib
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from cryptography.fernet import Fernet

from src.storage.drive_storage import DriveStorage
from src.config import expected_transaction_month, service_by_id
from src.ui import styles as ui_styles_module


class FakeDriveStorage:
    def __init__(self, files: list[dict[str, str]]):
        self.files = files
        # The month-outcome record lives in the same folder and reaches Drive
        # through this, so it has to exist for the app to keep any status.
        self.service = MagicMock()
        self.folder_id = "folder-1"

    def list_files(self) -> list[dict[str, str]]:
        return list(self.files)


class FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeBrowserLeaseRegistry:
    def __init__(self) -> None:
        self.token = "opaque-test-token"
        self.lease = None
        self.service_id = ""
        self.target_month = ""
        self.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.discard_calls: list[str] = []

    def create(self, *, service_id, target_month, browser, run_dir):
        self.service_id = service_id
        self.target_month = target_month
        self.lease = SimpleNamespace(browser=browser, run_dir=run_dir)
        return SimpleNamespace(token=self.token, expires_at=self.expires_at)

    def metadata(self, token):
        if token != self.token or self.lease is None:
            raise RuntimeError("lease unavailable")
        return SimpleNamespace(
            service_id=self.service_id,
            target_month=self.target_month,
            expires_at=self.expires_at,
        )

    @contextmanager
    def checkout(self, token, *, expected_service_id=None, expected_target_month=None):
        if token != self.token or self.lease is None:
            raise RuntimeError("lease unavailable")
        if expected_service_id != self.service_id or expected_target_month != self.target_month:
            raise RuntimeError("lease mismatch")
        yield self.lease

    def discard(self, token):
        self.discard_calls.append(token)
        if token == self.token:
            self.lease = None
            return True
        return False


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
        access_patcher = patch(
            "src.ui.access_control.require_owner_access",
            return_value=None,
        )
        access_patcher.start()
        self.addCleanup(access_patcher.stop)
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

    def test_archive_filters_only_by_month_and_search(self) -> None:
        files = [
            drive_file("20260530_Anthropic_$10-$10.pdf", file_id="anthropic-refund"),
            drive_file("20260530_OpenAI_1000円.pdf", file_id="openai-standard"),
            drive_file("20260615_OpenAI_$8.pdf", file_id="openai-june"),
            drive_file("20260708_交通サービス_500円.pdf", file_id="transport"),
        ]
        app = self.app()
        with patch.object(DriveStorage, "from_secrets", return_value=FakeDriveStorage(files)):
            app.run(timeout=20)
            app.segmented_control[0].set_value("単発領収書").run(timeout=20)

            self.assertEqual(["取引月"], [selectbox.label for selectbox in app.selectbox])
            app.selectbox[0].set_value("2026-05").run(timeout=20)
            markdown = "\n".join(item.value for item in app.markdown)
            self.assertIn("Anthropic", markdown)
            self.assertIn("OpenAI", markdown)
            self.assertNotIn("交通サービス", markdown)

            app.text_input[0].set_value("anthropic").run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("<strong>1</strong><span>/ 4 件を表示</span>", markdown)
        self.assertIn("20260530_Anthropic_$10-$10.pdf", markdown)
        self.assertIn("USD / $", markdown)
        self.assertIn("返金あり", markdown)
        self.assertNotIn("20260530_OpenAI_1000円.pdf", markdown)
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

    def test_email_security_code_pauses_and_resumes_the_same_browser(self) -> None:
        files = [
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
        ]
        storage = FakeDriveStorage(files)
        registry = FakeBrowserLeaseRegistry()
        browser = MagicMock()
        resumed: list[tuple[str, str]] = []

        class ResumableFetcher:
            def resume_after_security_code(self, target_month, code):
                resumed.append((target_month, code))
                return object()

        fetcher = ResumableFetcher()
        calls = 0

        def acquire(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    success=False,
                    action_required=True,
                    challenge=SimpleNamespace(
                        kind="verification_code",
                        message="メール確認コードの入力が必要です。",
                    ),
                    failure=None,
                )
            kwargs["fetch_statement"](kwargs["target_month"])
            if calls == 2:
                return SimpleNamespace(
                    success=False,
                    action_required=True,
                    challenge=SimpleNamespace(
                        kind="verification_code",
                        message="確認コードが拒否されました。",
                    ),
                    failure=None,
                )
            transaction_month = expected_transaction_month("commufa", kwargs["target_month"])
            file = receipt_file(
                f"{transaction_month.replace('-', '')}01",
                service_by_id("commufa").default_partner,
                5720,
            )
            storage.files.append(file)
            return SimpleNamespace(
                success=True,
                action_required=False,
                failure=None,
                file_name=file["name"],
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser", return_value=browser),
            patch("src.automation.providers.build_receipt_fetcher", return_value=fetcher),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch(
                "src.automation.security_challenge.browser_lease_registry",
                registry,
            ),
        ):
            app.run(timeout=20)
            acquisition_button = next(button for button in app.button if "自動取得" in button.label)
            acquisition_button.click().run(timeout=20)

            self.assertEqual(["メールに届いた確認コード"], [item.label for item in app.text_input])
            self.assertTrue(any("自動取得は終了せず待機" in item.value for item in app.warning))
            self.assertFalse(any("失敗したため終了" in item.value for item in app.markdown))
            self.assertEqual(
                "awaiting_security_code",
                app.session_state["getreceipt_batch"]["phase"],
            )
            browser.close.assert_not_called()

            app.text_input[0].set_value("123")
            invalid_submit = next(
                button for button in app.button if button.label == "認証して自動取得を続行"
            )
            invalid_submit.click().run(timeout=20)
            self.assertEqual(calls, 1)
            self.assertTrue(any("6桁の数字" in item.value for item in app.error))
            browser.close.assert_not_called()

            app.text_input[0].set_value("123456")
            submit = next(button for button in app.button if button.label == "認証して自動取得を続行")
            submit.click().run(timeout=20)

            self.assertEqual(calls, 2)
            self.assertTrue(any("最新の6桁コード" in item.value for item in app.error))
            self.assertEqual([], registry.discard_calls)
            browser.close.assert_not_called()

            app.text_input[0].set_value("654321")
            retry = next(button for button in app.button if button.label == "認証して自動取得を続行")
            retry.click().run(timeout=20)

        self.assertEqual(calls, 3)
        self.assertEqual(
            resumed,
            [("2026-07", "123456"), ("2026-07", "654321")],
        )
        self.assertEqual(registry.discard_calls, [registry.token])
        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", markdown)
        self.assertNotIn("123456", str(app.session_state))
        self.assertNotIn("654321", str(app.session_state))

    def test_remote_worker_challenge_recovers_from_api_and_does_not_retain_code(self) -> None:
        files = [
            receipt_file("20260605", "株式会社エポスカード", 10001),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
        ]
        storage = FakeDriveStorage(files)
        app = self.app()
        app.secrets["receipt_worker"] = {
            "base_url": "https://worker.example.test",
            "api_token": "t" * 48,
            "owner_id": "owner-1",
        }
        responded = False
        submitted_bodies: list[dict] = []
        waiting_job = {
            "id": "job-remote-1",
            "target_month": "2026-07",
            "service_ids": ["commufa"],
            "completed_service_ids": [],
            "current_service_id": "commufa",
            "state": "waiting_for_challenge",
            "version": 2,
            "created_at": "2026-07-19T01:00:00+00:00",
            "updated_at": "2026-07-19T01:01:00+00:00",
            "error": None,
            "result": None,
            "challenge": {
                "id": "challenge-remote-1",
                "kind": "otp_email",
                "message": "メールの確認コードを入力してください。",
                "input_schema": {
                    "input_type": "code",
                    "label": "メールに届いた6桁の確認コード",
                    "required": True,
                    "min_length": 6,
                    "max_length": 6,
                    "pattern": r"^[0-9]{6}$",
                    "autocomplete": "one-time-code",
                },
                "expires_at": "2026-07-19T01:10:00+00:00",
            },
        }

        def request(method, url, **kwargs):
            nonlocal responded
            if method == "POST" and url.endswith("/respond"):
                submitted_bodies.append(dict(kwargs["json"]))
                responded = True
                return FakeHTTPResponse(200, {**waiting_job, "state": "running", "challenge": None})
            if method == "GET" and (
                url.endswith("/v1/jobs/active") or url.endswith("/v1/jobs/job-remote-1")
            ):
                if responded:
                    return FakeHTTPResponse(
                        200,
                        {**waiting_job, "state": "running", "challenge": None, "version": 3},
                    )
                return FakeHTTPResponse(200, waiting_job)
            raise AssertionError(f"unexpected worker request: {method} {url}")

        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("requests.Session.request", side_effect=request),
            patch("src.ui.access_control.require_owner_access", return_value=None),
        ):
            app.run(timeout=20)
            self.assertEqual(
                ["メールに届いた6桁の確認コード"],
                [item.label for item in app.text_input],
            )
            app.text_input[0].set_value("１２３４５６")
            next(
                button for button in app.button if button.label == "本人確認を続行"
            ).click().run(timeout=20)

        self.assertEqual([{"response": "123456"}], submitted_bodies)
        self.assertNotIn("123456", str(app.session_state))
        self.assertNotIn("１２３４５６", str(app.session_state))
        self.assertEqual([], list(app.exception))
        self.assertTrue(
            any(button.label == "最新状態に更新" for button in app.button)
        )

    def test_first_failure_still_runs_following_services(self) -> None:
        storage = FakeDriveStorage([])
        attempted: list[str] = []

        def acquire(**kwargs):
            service_id = kwargs["service_id"]
            attempted.append(service_id)
            if service_id == "epos":
                return SimpleNamespace(
                    success=False,
                    file_name="",
                    failure=SimpleNamespace(
                        code="LOGIN_FAILED",
                        message="ログインに失敗しました。",
                    ),
                )
            return SimpleNamespace(success=True, file_name="ok.pdf", failure=None)

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
        ):
            app.run(timeout=20)
            acquisition_button = next(button for button in app.button if "自動取得" in button.label)
            acquisition_button.click().run(timeout=60)

        # A failed epos must not stop the remaining services from being tried.
        self.assertIn("epos", attempted)
        self.assertGreater(len(attempted), 1)
        self.assertEqual([], list(app.exception))

    def _drive_dead(self):
        def refuse(*args, **kwargs):
            raise RuntimeError("('invalid_grant: Token has been expired or revoked.')")

        return refuse

    def _app_with_encryption_key(self):
        app = self.app()
        app.secrets = dict(app.secrets) | {
            "microsoft_graph": {"encryption_key": Fernet.generate_key().decode("ascii")},
            "google_oauth": {
                "client_id": "client.apps.googleusercontent.com",
                "client_secret": "secret",
                "refresh_token": "1//0e-the-stale-one-in-secrets",
            },
        }
        return app

    def test_a_stale_secret_is_recovered_from_the_stored_credential(self) -> None:
        """Secrets can only be edited by hand, so a stale one must not stop the app.

        The credential kept on the last reconnection is read back through the
        service account key, which never expires.
        """

        files = [receipt_file("20260605", "株式会社エポスカード", 10001)]
        app = self._app_with_encryption_key()
        with (
            patch.object(DriveStorage, "from_secrets", side_effect=self._drive_dead()),
            patch.object(DriveStorage, "list_files", return_value=files),
            patch("src.storage.drive_storage.build_drive_service", return_value=MagicMock()),
            patch("src.storage.drive_storage.build_user_drive_service", return_value=MagicMock()),
            patch(
                "src.storage.google_credential_store.GoogleCredentialStore.load",
                return_value="1//0e-recovered-refresh-token-value",
            ),
        ):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = chr(10).join(item.value for item in app.markdown)
        self.assertNotIn("DRIVE_CONNECTION_FAILED", markdown)
        self.assertIn("株式会社エポスカード", markdown)

    def test_without_a_stored_credential_the_reconnect_card_is_offered(self) -> None:
        app = self._app_with_encryption_key()
        with (
            patch.object(DriveStorage, "from_secrets", side_effect=self._drive_dead()),
            patch("src.storage.drive_storage.build_drive_service", return_value=MagicMock()),
            patch(
                "src.storage.google_credential_store.GoogleCredentialStore.load",
                return_value="",
            ),
        ):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        markdown = chr(10).join(item.value for item in app.markdown)
        self.assertIn("DRIVE_CONNECTION_FAILED", markdown)
        # The owner is offered the reconnection instead of being left stuck.
        self.assertIn("Google Driveを接続し直す", markdown)
        self.assertTrue(any("承認後のURL" in str(item.label) for item in app.text_input))

    def test_a_stale_reconnect_module_is_reloaded_before_it_is_called(self) -> None:
        """This card is only ever reached when Drive is already down.

        A deploy can leave the previous version of it in sys.modules, and a
        crash here would take away the one screen that exists to recover.
        """

        import src.ui.google_link as google_link_module

        app = self._app_with_encryption_key()
        google_link_module.UI_API_VERSION = 1
        stale = google_link_module.render_google_reconnect

        def old_signature(st_module, secrets):  # no "remember" parameter
            raise AssertionError("the stale module was called")

        google_link_module.render_google_reconnect = old_signature
        try:
            with (
                patch.object(DriveStorage, "from_secrets", side_effect=self._drive_dead()),
                patch("src.storage.drive_storage.build_drive_service", return_value=MagicMock()),
                patch(
                    "src.storage.google_credential_store.GoogleCredentialStore.load",
                    return_value="",
                ),
            ):
                app.run(timeout=20)
        finally:
            google_link_module.render_google_reconnect = stale
            importlib.reload(google_link_module)

        self.assertEqual([], list(app.exception))
        markdown = chr(10).join(item.value for item in app.markdown)
        self.assertIn("Google Driveを接続し直す", markdown)

    def test_sign_ins_are_capped_so_codes_stop_arriving(self) -> None:
        """Each browser sign-in mails the owner another verification code.

        Nothing may keep signing in on its own: it spams the owner and
        repeated sign-ins are what risks an account lock.
        """

        storage = FakeDriveStorage([])
        attempts: list[str] = []

        def acquire(**kwargs):
            attempts.append(kwargs["service_id"])
            return SimpleNamespace(
                success=False,
                action_required=False,
                file_name="",
                failure=SimpleNamespace(
                    code="LOGIN_TIMEOUT", message="ログインできませんでした。", detail=""
                ),
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
        ):
            app.run(timeout=20)
            for _ in range(4):
                buttons = [b for b in app.button if "自動取得" in b.label]
                if not buttons:
                    break
                buttons[0].click().run(timeout=60)

        commufa_signins = attempts.count("commufa")
        self.assertGreater(commufa_signins, 0)
        self.assertLessEqual(commufa_signins, 2)
        markdown = chr(10).join(item.value for item in app.markdown)
        self.assertIn("中止しました", markdown)
        self.assertEqual([], list(app.exception))

    def test_a_status_survives_reopening_the_app(self) -> None:
        """Session state is gone on reload, so the reason has to outlive it.

        Otherwise the screen forgets the run the owner just watched, and the
        status can only ever describe the last few minutes.
        """

        storage = FakeDriveStorage([])
        remembered = {
            "2026-07": {
                "commufa": {
                    "code": "COMMUFA_MONTH_NOT_ISSUED",
                    "message": "コミュファに2026年8月分の利用明細がまだ掲載されていません。",
                    "detail": "掲載済みの最新は2026年7月分です。",
                    "at": "2026-08-01T12:00:00+00:00",
                }
            }
        }

        # A brand-new session: nothing in session state, everything from Drive.
        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch(
                "src.storage.status_store.ServiceStatusStore.load",
                return_value=remembered,
            ),
        ):
            app.run(timeout=20)

        self.assertEqual([], list(app.exception))
        cards = [str(item.value) for item in app.markdown if "gr-card" in str(item.value)]
        commufa = next(c for c in cards if "中部テレコミュニケーション" in c)
        self.assertIn("gr-card--not_issued", commufa)
        self.assertIn("まだ掲載されていません", commufa)

    def test_a_saved_month_forgets_its_remembered_reason(self) -> None:
        storage = FakeDriveStorage([])

        def acquire(**kwargs):
            transaction = expected_transaction_month(kwargs["service_id"], kwargs["target_month"])
            file = receipt_file(
                f"{transaction.replace('-', '')}01",
                service_by_id(kwargs["service_id"]).default_partner,
                1000,
            )
            storage.files.append(file)
            return SimpleNamespace(
                success=True, action_required=False, failure=None, file_name=file["name"]
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch("src.storage.status_store.ServiceStatusStore.load", return_value={}),
            patch("src.storage.status_store.ServiceStatusStore.clear") as forget,
        ):
            app.run(timeout=20)
            next(b for b in app.button if "自動取得" in b.label).click().run(timeout=60)

        self.assertEqual([], list(app.exception))
        cleared = {call.kwargs["service_id"] for call in forget.call_args_list}
        self.assertIn("commufa", cleared)

    def test_a_saved_month_does_not_inherit_its_sign_in_count(self) -> None:
        """The cap protects against repeats, not against using the app.

        Without clearing it, one session that took two sign-ins would refuse
        to run that service again even after it had succeeded.
        """

        storage = FakeDriveStorage([])
        attempts: list[str] = []

        def acquire(**kwargs):
            attempts.append(kwargs["service_id"])
            transaction = expected_transaction_month(kwargs["service_id"], kwargs["target_month"])
            file = receipt_file(
                f"{transaction.replace('-', '')}01",
                service_by_id(kwargs["service_id"]).default_partner,
                1000,
            )
            storage.files.append(file)
            return SimpleNamespace(
                success=True, action_required=False, failure=None, file_name=file["name"]
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch("src.storage.status_store.ServiceStatusStore.load", return_value={}),
            patch("src.storage.status_store.ServiceStatusStore.clear"),
        ):
            app.run(timeout=20)
            next(b for b in app.button if "自動取得" in b.label).click().run(timeout=60)

        self.assertEqual([], list(app.exception))
        counts = app.session_state["getreceipt_signin_attempts"]
        self.assertEqual({}, {k: v for k, v in counts.items() if v})

    def test_an_unbilled_month_does_not_count_against_the_sign_in_cap(self) -> None:
        """The provider answered fine; there is simply no bill yet.

        Counting that as a failed sign-in locked the service out of the
        session for a month where nothing was wrong.
        """

        storage = FakeDriveStorage([])
        attempts: list[str] = []

        def acquire(**kwargs):
            attempts.append(kwargs["service_id"])
            return SimpleNamespace(
                success=False,
                action_required=False,
                file_name="",
                failure=SimpleNamespace(
                    code="COMMUFA_MONTH_NOT_ISSUED",
                    message="まだ掲載されていません。",
                    detail="請求が確定してから再実行してください。",
                ),
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch("src.storage.status_store.ServiceStatusStore.load", return_value={}),
            patch("src.storage.status_store.ServiceStatusStore.record"),
            patch("src.storage.status_store.ServiceStatusStore.clear"),
        ):
            app.run(timeout=20)
            for _ in range(4):
                buttons = [b for b in app.button if "自動取得" in b.label]
                if not buttons:
                    break
                buttons[0].click().run(timeout=60)

        # It keeps being tried, and never reports the cap.
        self.assertGreater(attempts.count("commufa"), 2)
        markdown = chr(10).join(item.value for item in app.markdown)
        self.assertNotIn("SIGNIN_ATTEMPT_LIMIT", markdown)
        self.assertEqual([], list(app.exception))

    def test_a_puzzle_holds_the_browser_open_instead_of_ending_the_job(self) -> None:
        """Epos guards its sign-in with a slide puzzle.

        The app must not answer it, but it must not throw the acquisition away
        either: the same Chrome is held open and mirrored so the owner can work
        the control, and everything after it stays automatic.
        """

        files = [
            receipt_file("20260812", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
        ]
        storage = FakeDriveStorage(files)
        registry = FakeBrowserLeaseRegistry()
        browser = MagicMock()
        browser.current_page_target.return_value = {
            "url": "https://www.eposcard.co.jp/memberservice/pc/nocardusedetail/login_dispatch.do"
        }
        resumed: list[str] = []

        class PuzzleFetcher:
            """No resume_after_security_code: a puzzle has no code to send."""

            def resume_after_interactive_challenge(self, target_month):
                resumed.append(target_month)
                return object()

        calls = 0

        def acquire(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(
                    success=False,
                    action_required=True,
                    challenge=SimpleNamespace(
                        kind="captcha",
                        message="画像認証（パズル）が表示されました。",
                    ),
                    failure=None,
                )
            kwargs["fetch_statement"](kwargs["target_month"])
            transaction_month = expected_transaction_month("epos", kwargs["target_month"])
            file = receipt_file(
                f"{transaction_month.replace('-', '')}05",
                service_by_id("epos").default_partner,
                87560,
            )
            storage.files.append(file)
            return SimpleNamespace(
                success=True, action_required=False, failure=None, file_name=file["name"]
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser", return_value=browser),
            patch(
                "src.automation.providers.build_receipt_fetcher",
                return_value=PuzzleFetcher(),
            ),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch("src.automation.security_challenge.browser_lease_registry", registry),
            patch("src.ui.live_view.render_live_view") as live_view,
        ):
            app.run(timeout=20)
            next(b for b in app.button if "自動取得" in b.label).click().run(timeout=20)

            # No code box is offered, and the page is not mirrored until asked.
            self.assertEqual([], [item.label for item in app.text_input])
            live_view.assert_not_called()
            self.assertEqual(
                "awaiting_security_code",
                app.session_state["getreceipt_batch"]["phase"],
            )
            browser.close.assert_not_called()

            open_button = next(b for b in app.button if b.label == "🧩 パズルを開く")
            open_button.click().run(timeout=20)

            live_view.assert_called()
            self.assertEqual(
                ("www.eposcard.co.jp",),
                live_view.call_args.kwargs["allowed_hosts"],
            )
            browser.close.assert_not_called()

            resume_button = next(
                b for b in app.button if b.label == "🧩 解除して自動取得を続ける"
            )
            resume_button.click().run(timeout=20)

        self.assertEqual(["2026-07"], resumed)
        self.assertEqual(registry.discard_calls, [registry.token])
        self.assertEqual([], list(app.exception))
        markdown = "\n".join(item.value for item in app.markdown)
        self.assertIn("4件すべてのPDFをGoogle Driveで確認しました", markdown)

    def test_an_unfinished_puzzle_keeps_the_same_browser_and_reopens(self) -> None:
        """Pressing 解除 too early must not throw the sign-in away.

        A new sign-in means a new puzzle and another login attempt, so an
        unfinished piece keeps the same Chrome and simply shows it again.
        """

        files = [
            receipt_file("20260812", "中部テレコミュニケーション株式会社", 10002),
            receipt_file("20260812", "フラットエナジー株式会社", 10003),
            receipt_file("20260709", "NTTファイナンス株式会社", 10004),
        ]
        storage = FakeDriveStorage(files)
        registry = FakeBrowserLeaseRegistry()
        browser = MagicMock()

        class PuzzleFetcher:
            def resume_after_interactive_challenge(self, target_month):
                return object()

        def acquire(**kwargs):
            fetch = kwargs.get("fetch_statement")
            if fetch is not None:
                fetch(kwargs["target_month"])
            return SimpleNamespace(
                success=False,
                action_required=True,
                challenge=SimpleNamespace(
                    kind="captcha",
                    message="パズルがまだ完成していません。",
                ),
                failure=None,
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser", return_value=browser),
            patch(
                "src.automation.providers.build_receipt_fetcher",
                return_value=PuzzleFetcher(),
            ),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
            patch("src.automation.security_challenge.browser_lease_registry", registry),
            patch("src.ui.live_view.render_live_view"),
        ):
            app.run(timeout=20)
            next(b for b in app.button if "自動取得" in b.label).click().run(timeout=20)
            next(b for b in app.button if b.label == "🧩 パズルを開く").click().run(timeout=20)
            next(
                b for b in app.button if b.label == "🧩 解除して自動取得を続ける"
            ).click().run(timeout=20)

        self.assertTrue(any("パズル" in item.value for item in app.error))
        # The browser is still held, and the puzzle is shown again straight away.
        self.assertEqual([], registry.discard_calls)
        browser.close.assert_not_called()
        self.assertTrue(
            any(b.label == "🧩 解除して自動取得を続ける" for b in app.button)
        )
        self.assertEqual([], list(app.exception))

    def test_a_month_the_provider_has_not_billed_is_not_shown_as_a_failure(self) -> None:
        """Wi-Fi and electricity bill the month after use.

        Asking for the current month before the bill exists is the normal
        case, so it must not be painted red alongside real breakages.
        """

        storage = FakeDriveStorage([])

        def acquire(**kwargs):
            if kwargs["service_id"] == "commufa":
                return SimpleNamespace(
                    success=False,
                    file_name="",
                    failure=SimpleNamespace(
                        code="COMMUFA_MONTH_NOT_ISSUED",
                        message="コミュファに2026年8月分の利用明細がまだ掲載されていません。",
                        detail="掲載済みの最新は2026年7月分です。",
                    ),
                )
            if kwargs["service_id"] == "tokuten":
                return SimpleNamespace(
                    success=False,
                    file_name="",
                    failure=SimpleNamespace(
                        code="TOKUTEN_GRAPH_ATTACHMENT_NOT_FOUND",
                        message="トクテンでんきの2026年8月分の請求メールがまだ見つかりません。",
                        detail="請求が確定して添付PDFが届いてから再実行してください。",
                    ),
                )
            return SimpleNamespace(
                success=False,
                file_name="",
                failure=SimpleNamespace(
                    code="LOGIN_FAILED", message="ログインに失敗しました。", detail=""
                ),
            )

        app = self.app()
        with (
            patch.object(DriveStorage, "from_secrets", return_value=storage),
            patch("src.automation.browser_session.ManagedBrowser"),
            patch("src.automation.providers.build_receipt_fetcher", return_value=object()),
            patch("src.workflows.auto_acquisition.run_auto_acquisition", side_effect=acquire),
        ):
            app.run(timeout=20)
            next(b for b in app.button if "自動取得" in b.label).click().run(timeout=60)

        cards = [str(item.value) for item in app.markdown if "gr-card" in str(item.value)]
        commufa = next(c for c in cards if "中部テレコミュニケーション" in c)

        self.assertIn("gr-card--not_issued", commufa)
        self.assertIn("未発行", commufa)
        self.assertNotIn("COMMUFA_MONTH_NOT_ISSUED", commufa)
        # The card reads as a status, not as something that went wrong.
        self.assertNotIn("失敗", commufa)

        # Electricity bills a month behind too, and must read the same way.
        tokuten = next(c for c in cards if "フラットエナジー" in c)
        self.assertIn("gr-card--not_issued", tokuten)
        self.assertNotIn("TOKUTEN_GRAPH_ATTACHMENT_NOT_FOUND", tokuten)
        self.assertNotIn("失敗", tokuten)

        # A genuine breakage still reads as a failure.
        self.assertIn("gr-card--failed", next(c for c in cards if "エポスカード" in c))


if __name__ == "__main__":
    unittest.main()
