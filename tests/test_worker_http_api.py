from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.workers.http_api import WorkerASGIApp  # noqa: E402


class FakeWorker:
    running = True
    active_job_id = ""

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class FakeService:
    def __init__(self) -> None:
        self.worker = FakeWorker()
        self.responses: list[str] = []
        self.viewer_actions: list[dict] = []
        self.manual_uploads: list[dict] = []

    def authorize_owner(self, owner_id):
        if owner_id != "owner-1":
            from src.workers.service import WorkerServiceError

            raise WorkerServiceError(
                "forbidden",
                code="OWNER_FORBIDDEN",
                status_code=403,
            )

    def health(self, *, owner_id):
        if owner_id != "owner-1":
            raise AssertionError("owner header mismatch")
        return {"status": "ok"}

    def create_job(self, **kwargs):
        return {
            "id": "job-1",
            "state": "queued",
            "target_month": kwargs["target_month"],
            "service_ids": kwargs["service_ids"],
        }

    def find_active_job(self, **_kwargs):
        return None

    def get_job(self, job_id, **_kwargs):
        return {"id": job_id, "state": "running"}

    def submit_challenge_response(self, **kwargs):
        self.responses.append(kwargs["response"])
        return {"id": kwargs["job_id"], "state": "running"}

    def cancel_job(self, job_id, **_kwargs):
        return {"id": job_id, "state": "cancelled"}

    def viewer_frame(self, **_kwargs):
        return b"\x89PNG\r\n\x1a\nframe"

    def send_viewer_input(self, **kwargs):
        self.viewer_actions.append(kwargs)
        return {"id": kwargs["job_id"], "state": "waiting_for_challenge"}

    def complete_interactive_challenge(self, **kwargs):
        return {"id": kwargs["job_id"], "state": "running"}

    def microsoft_oauth_status(self, **_kwargs):
        return {"configured": True, "connected": False}

    def start_microsoft_oauth(self, **_kwargs):
        return {
            "authorization_url": (
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
            )
        }

    def complete_microsoft_oauth(self, **_kwargs):
        return {"configured": True, "connected": True}

    def disconnect_microsoft_oauth(self, **_kwargs):
        return {"configured": True, "connected": False}

    def save_manual_receipt(self, **kwargs):
        self.authorize_owner(kwargs["owner_id"])
        self.manual_uploads.append(
            {
                "service_id": kwargs["service_id"],
                "target_month": kwargs["target_month"],
                "confirmed": kwargs["confirmed"],
                "size": len(kwargs["content"]),
                "is_pdf": kwargs["content"].startswith(b"%PDF"),
            }
        )
        return {
            "success": True,
            "service_id": kwargs["service_id"],
            "target_month": kwargs["target_month"],
            "status": "acquired",
            "skipped": False,
            "receipt": {"file_id": "drive-1", "file_name": "receipt.pdf"},
        }


class WorkerHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.token = "t" * 48
        self.app = WorkerASGIApp(service=self.service, api_token=self.token)

    def request(
        self,
        method: str,
        path: str,
        *,
        body=None,
        raw_body=None,
        token=None,
        query="",
        headers=None,
        owner="owner-1",
    ):
        async def run():
            request_body = (
                bytes(raw_body)
                if raw_body is not None
                else (
                    json.dumps(body).encode("utf-8")
                    if body is not None
                    else b""
                )
            )
            messages = [
                {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }
            ]
            sent = []

            async def receive():
                return messages.pop(0)

            async def send(message):
                sent.append(message)

            authorization = token if token is not None else self.token
            request_headers = [
                (b"authorization", f"Bearer {authorization}".encode()),
                (b"x-getreceipt-owner", owner.encode()),
            ]
            request_headers.extend(
                (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
                for key, value in (headers or {}).items()
            )
            await self.app(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "query_string": query.encode("utf-8"),
                    "headers": request_headers,
                },
                receive,
                send,
            )
            status = sent[0]["status"]
            response_headers = dict(sent[0]["headers"])
            if response_headers.get(b"content-type") == b"image/png":
                payload = bytes(sent[1]["body"])
            else:
                payload = json.loads(sent[1]["body"].decode("utf-8"))
            return status, payload, response_headers

        return asyncio.run(run())

    def test_auth_is_required_and_response_is_no_store(self) -> None:
        status, payload, _headers = self.request(
            "GET",
            "/healthz",
            token="wrong-token-value-that-is-long-enough",
        )
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHORIZED", payload["error"]["code"])

        status, payload, headers = self.request("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(b"no-store", headers[b"cache-control"])

    def test_create_and_recover_routes(self) -> None:
        status, payload, _headers = self.request(
            "POST",
            "/v1/jobs",
            body={
                "target_month": "2026-07",
                "service_ids": ["epos", "commufa"],
                "idempotency_key": "mobile-contract",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("job-1", payload["id"])

        status, payload, _headers = self.request("GET", "/v1/jobs/job-1")
        self.assertEqual(200, status)
        self.assertEqual("job-1", payload["id"])

        status, payload, _headers = self.request(
            "GET",
            "/v1/jobs/active",
            query="target_month=2026-07",
        )
        self.assertEqual(404, status)
        self.assertEqual("JOB_NOT_FOUND", payload["error"]["code"])

    def test_challenge_value_is_only_forwarded_to_service(self) -> None:
        status, payload, _headers = self.request(
            "POST",
            "/v1/jobs/job-1/challenges/challenge-1/respond",
            body={"response": "654321"},
        )
        self.assertEqual(200, status)
        self.assertEqual("running", payload["state"])
        self.assertEqual(["654321"], self.service.responses)
        self.assertNotIn("654321", json.dumps(payload))

    def test_large_or_invalid_body_is_rejected_without_echo(self) -> None:
        status, payload, _headers = self.request(
            "POST",
            "/v1/jobs",
            body={"oversized": "x" * (17 * 1024)},
        )
        self.assertEqual(400, status)
        self.assertEqual("INVALID_REQUEST", payload["error"]["code"])
        self.assertNotIn("x" * 100, json.dumps(payload))

    def test_manual_pdf_route_accepts_raw_body_above_json_limit(self) -> None:
        content = b"%PDF-1.7\n" + (b"x" * (17 * 1024))
        status, payload, headers = self.request(
            "POST",
            "/v1/manual-receipts",
            raw_body=content,
            query="service_id=epos&target_month=2026-07&confirmed=true",
            headers={
                "content-type": "application/pdf",
                "content-length": str(len(content)),
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["success"])
        self.assertEqual(b"no-store", headers[b"cache-control"])
        self.assertEqual(
            {
                "service_id": "epos",
                "target_month": "2026-07",
                "confirmed": True,
                "size": len(content),
                "is_pdf": True,
            },
            self.service.manual_uploads[0],
        )
        self.assertNotIn("x" * 100, json.dumps(payload))

    def test_manual_pdf_route_rejects_oversize_and_wrong_owner_before_body(self) -> None:
        status, payload, _headers = self.request(
            "POST",
            "/v1/manual-receipts",
            raw_body=b"",
            query="service_id=epos&target_month=2026-07&confirmed=false",
            headers={
                "content-type": "application/pdf",
                "content-length": str((20 * 1024 * 1024) + 1),
            },
        )
        self.assertEqual(413, status)
        self.assertEqual("MANUAL_UPLOAD_TOO_LARGE", payload["error"]["code"])

        status, payload, _headers = self.request(
            "POST",
            "/v1/manual-receipts",
            raw_body=b"%PDF secret-body",
            owner="owner-2",
            query="service_id=epos&target_month=2026-07&confirmed=false",
            headers={"content-type": "application/pdf"},
        )
        self.assertEqual(403, status)
        self.assertEqual("OWNER_FORBIDDEN", payload["error"]["code"])
        self.assertEqual([], self.service.manual_uploads)

    def test_manual_pdf_streaming_cap_and_media_type_are_enforced(self) -> None:
        with patch("src.workers.http_api.MAX_MANUAL_PDF_BYTES", 10):
            status, payload, _headers = self.request(
                "POST",
                "/v1/manual-receipts",
                raw_body=b"%PDF" + (b"x" * 7),
                query="service_id=epos&target_month=2026-07&confirmed=false",
                headers={"content-type": "application/pdf"},
            )
        self.assertEqual(413, status)
        self.assertEqual("MANUAL_UPLOAD_TOO_LARGE", payload["error"]["code"])

        status, payload, _headers = self.request(
            "POST",
            "/v1/manual-receipts",
            raw_body=b"%PDF content",
            query="service_id=epos&target_month=2026-07&confirmed=false",
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(415, status)
        self.assertEqual(
            "MANUAL_UPLOAD_MEDIA_TYPE_INVALID",
            payload["error"]["code"],
        )

    def test_interactive_viewer_routes_return_no_store_png_and_forward_input(self) -> None:
        base = "/v1/jobs/job-1/challenges/challenge-1/viewer"
        status, payload, headers = self.request("GET", f"{base}/frame")
        self.assertEqual(200, status)
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(b"no-store", headers[b"cache-control"])

        status, payload, _headers = self.request(
            "POST",
            f"{base}/input",
            body={"action": "click", "x": 20, "y": 30},
        )
        self.assertEqual(200, status)
        self.assertEqual("waiting_for_challenge", payload["state"])
        self.assertEqual(20, self.service.viewer_actions[0]["x"])

        status, payload, _headers = self.request("POST", f"{base}/complete")
        self.assertEqual(200, status)
        self.assertEqual("running", payload["state"])

    def test_microsoft_oauth_routes_do_not_echo_callback_code(self) -> None:
        status, payload, _headers = self.request(
            "GET",
            "/v1/oauth/microsoft/status",
        )
        self.assertEqual(200, status)
        self.assertFalse(payload["connected"])
        status, payload, _headers = self.request(
            "POST",
            "/v1/oauth/microsoft/complete",
            body={"code": "c" * 40, "state": "s" * 40},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["connected"])
        self.assertNotIn("c" * 40, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
