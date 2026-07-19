from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))

from src.jobs.client import (  # noqa: E402
    WorkerApiError,
    WorkerClient,
    WorkerConfigError,
    WorkerConnection,
    bearer_token_matches,
    worker_connection_from_secrets,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, *, content=b"", headers=None) -> None:
        self.status_code = status_code
        self.payload = payload
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class WorkerClientTest(unittest.TestCase):
    def connection(self) -> WorkerConnection:
        return WorkerConnection(
            base_url="https://worker.example.test/",
            api_token="t" * 48,
            owner_id="owner-1",
        )

    def test_connection_rejects_non_https_remote_url_and_short_token(self) -> None:
        with self.assertRaises(WorkerConfigError):
            WorkerConnection(
                base_url="http://worker.example.test",
                api_token="t" * 48,
                owner_id="owner-1",
            )
        with self.assertRaises(WorkerConfigError):
            WorkerConnection(
                base_url="https://worker.example.test",
                api_token="short",
                owner_id="owner-1",
            )

    def test_local_http_is_allowed_for_contract_tests(self) -> None:
        connection = WorkerConnection(
            base_url="http://127.0.0.1:8765/",
            api_token="t" * 48,
            owner_id="owner-1",
        )
        self.assertEqual(connection.base_url, "http://127.0.0.1:8765")

    def test_worker_secrets_are_all_or_nothing(self) -> None:
        self.assertIsNone(worker_connection_from_secrets({}))
        with self.assertRaises(WorkerConfigError):
            worker_connection_from_secrets(
                {"receipt_worker": {"base_url": "https://worker.example.test"}}
            )
        configured = worker_connection_from_secrets(
            {
                "receipt_worker": {
                    "base_url": "https://worker.example.test",
                    "api_token": "x" * 48,
                    "owner_id": "owner-1",
                }
            }
        )
        self.assertEqual(configured.owner_id, "owner-1")

    def test_create_job_sends_auth_and_owner_without_secret_in_url(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(200, {"id": "job-1", "state": "queued"})
        client = WorkerClient(self.connection(), session=session)

        result = client.create_job(
            target_month="2026-07",
            service_ids=["epos", "commufa"],
            idempotency_key="month-2026-07-epos-commufa",
        )

        self.assertEqual(result["id"], "job-1")
        kwargs = session.request.call_args.kwargs
        self.assertEqual(session.request.call_args.args[:2], ("POST", "https://worker.example.test/v1/jobs"))
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {'t' * 48}")
        self.assertEqual(kwargs["headers"]["X-GetReceipt-Owner"], "owner-1")
        self.assertNotIn("t" * 48, session.request.call_args.args[1])

    def test_challenge_response_is_only_in_json_body(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(
            200,
            {"id": "job-1", "state": "running"},
        )
        client = WorkerClient(self.connection(), session=session)

        client.submit_challenge_response(
            job_id="job-1",
            challenge_id="challenge-1",
            response="654321",
        )

        args = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        self.assertNotIn("654321", args[1])
        self.assertNotIn("654321", str(kwargs["headers"]))
        self.assertEqual(kwargs["json"], {"response": "654321"})

    def test_manual_receipt_uses_raw_pdf_body_and_safe_query_metadata(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(
            200,
            {
                "success": True,
                "service_id": "epos",
                "target_month": "2026-07",
                "status": "acquired",
                "skipped": False,
                "receipt": {"file_id": "drive-1", "file_name": "receipt.pdf"},
            },
        )
        client = WorkerClient(self.connection(), session=session)
        content = b"%PDF private receipt bytes"

        result = client.upload_manual_receipt(
            service_id="epos",
            target_month="2026-07",
            content=content,
            confirmed=True,
        )

        self.assertTrue(result["success"])
        args = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        self.assertEqual(
            args,
            ("POST", "https://worker.example.test/v1/manual-receipts"),
        )
        self.assertEqual(content, kwargs["data"])
        self.assertIsNone(kwargs["json"])
        self.assertEqual("application/pdf", kwargs["headers"]["Content-Type"])
        self.assertEqual(
            {
                "service_id": "epos",
                "target_month": "2026-07",
                "confirmed": "true",
            },
            kwargs["params"],
        )
        self.assertNotIn(content.decode(), args[1])
        self.assertNotIn(content.decode(), str(kwargs["headers"]))
        self.assertGreaterEqual(kwargs["timeout"], 120)

    def test_manual_receipt_api_error_does_not_echo_pdf(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(
            409,
            {
                "error": {
                    "code": "MANUAL_UPLOAD_BUSY",
                    "message": "自動取得を完了してください。",
                }
            },
        )
        client = WorkerClient(self.connection(), session=session)
        content = b"%PDF do-not-echo-this-payload"
        with self.assertRaises(WorkerApiError) as raised:
            client.upload_manual_receipt(
                service_id="epos",
                target_month="2026-07",
                content=content,
                confirmed=False,
            )
        self.assertEqual("MANUAL_UPLOAD_BUSY", raised.exception.code)
        self.assertNotIn("do-not-echo-this-payload", str(raised.exception))

    def test_sanitized_api_error_does_not_echo_request_body(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(
            409,
            {"error": {"code": "CHALLENGE_ALREADY_ANSWERED", "message": "回答済みです。"}},
        )
        client = WorkerClient(self.connection(), session=session)

        with self.assertRaises(WorkerApiError) as raised:
            client.submit_challenge_response(
                job_id="job-1",
                challenge_id="challenge-1",
                response="123456",
            )

        self.assertEqual(raised.exception.code, "CHALLENGE_ALREADY_ANSWERED")
        self.assertNotIn("123456", str(raised.exception))

    def test_active_job_not_found_returns_none(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(404, {"error": {"code": "NOT_FOUND"}})
        client = WorkerClient(self.connection(), session=session)
        self.assertIsNone(client.find_active_job("2026-07"))

    def test_viewer_frame_and_input_stay_on_authenticated_worker_api(self) -> None:
        session = MagicMock()
        session.request.side_effect = (
            FakeResponse(
                200,
                content=b"\x89PNG\r\n\x1a\nframe",
                headers={"content-type": "image/png"},
            ),
            FakeResponse(200, {"id": "job-1", "state": "waiting_for_challenge"}),
        )
        client = WorkerClient(self.connection(), session=session)
        frame = client.get_viewer_frame(
            job_id="job-1",
            challenge_id="challenge-1",
        )
        self.assertTrue(frame.startswith(b"\x89PNG"))
        client.send_viewer_input(
            job_id="job-1",
            challenge_id="challenge-1",
            action="click",
            x=100,
            y=200,
        )
        second = session.request.call_args_list[1]
        self.assertEqual(
            {
                "action": "click",
                "x": 100,
                "y": 200,
            },
            second.kwargs["json"],
        )
        self.assertNotIn("Authorization", second.args[1])

    def test_microsoft_authorization_url_must_be_official(self) -> None:
        session = MagicMock()
        session.request.return_value = FakeResponse(
            200,
            {
                "authorization_url": (
                    "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
                    "?state=opaque"
                )
            },
        )
        client = WorkerClient(self.connection(), session=session)
        self.assertIn(
            "login.microsoftonline.com",
            client.start_microsoft_oauth()["authorization_url"],
        )
        session.request.return_value = FakeResponse(
            200,
            {"authorization_url": "https://evil.example/phish"},
        )
        with self.assertRaises(WorkerApiError):
            client.start_microsoft_oauth()

    def test_bearer_token_comparison(self) -> None:
        token = "z" * 48
        self.assertTrue(bearer_token_matches(f"Bearer {token}", token))
        self.assertFalse(bearer_token_matches(f"Bearer {'y' * 48}", token))
        self.assertFalse(bearer_token_matches(token, token))


if __name__ == "__main__":
    unittest.main()
