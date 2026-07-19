from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.jobs.client import WorkerClient, WorkerConnection  # noqa: E402
from src.jobs.inbox import ChallengeResponseInbox  # noqa: E402
from src.jobs.store import SQLiteJobStore  # noqa: E402
from src.workers.http_api import WorkerASGIApp  # noqa: E402
from src.workers.runner import ReceiptWorker, WorkerRuntimeConfig  # noqa: E402
from src.workers.service import WorkerService  # noqa: E402


class AdapterResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class ASGISession:
    """requests-compatible adapter that keeps the real HTTP contract in-process."""

    def __init__(self, app: WorkerASGIApp) -> None:
        self.app = app

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        params: dict | None,
        json: dict | None,
        timeout: float,
    ) -> AdapterResponse:
        del timeout
        parsed = urlsplit(url)
        query = urlencode(params or {})

        async def run() -> AdapterResponse:
            incoming = [
                {
                    "type": "http.request",
                    "body": (
                        __import__("json").dumps(json).encode("utf-8")
                        if json is not None
                        else b""
                    ),
                    "more_body": False,
                }
            ]
            outgoing: list[dict] = []

            async def receive():
                return incoming.pop(0)

            async def send(message):
                outgoing.append(message)

            await self.app(
                {
                    "type": "http",
                    "method": method,
                    "path": parsed.path,
                    "query_string": query.encode("utf-8"),
                    "headers": [
                        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
                        for key, value in headers.items()
                    ],
                },
                receive,
                send,
            )
            return AdapterResponse(
                outgoing[0]["status"],
                __import__("json").loads(outgoing[1]["body"].decode("utf-8")),
            )

        return asyncio.run(run())


class FakeBrowser:
    def __init__(self, *, profile_dir: Path, download_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.download_dir = download_dir

    def close(self, *, clear_profile: bool = False) -> None:
        if clear_profile:
            raise AssertionError("persistent profile must not be cleared")


class FakeFetcher:
    def __init__(self) -> None:
        self.responses: list[str] = []

    def resume_after_security_code(self, target_month: str, code: str):
        self.responses.append(code)
        return SimpleNamespace(content=b"%PDF-1.7 fake")


class MobileWorkerEndToEndTest(unittest.TestCase):
    def test_client_api_reload_challenge_worker_and_durable_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SQLiteJobStore(root / "jobs.sqlite3")
            inbox = ChallengeResponseInbox()
            fetcher = FakeFetcher()

            def acquisition_runner(**kwargs):
                if "fetch_statement" not in kwargs:
                    return SimpleNamespace(
                        success=False,
                        action_required=True,
                        challenge=SimpleNamespace(
                            kind="verification_code",
                            message="iPhoneへ届いたコードを入力してください。",
                        ),
                        failure=None,
                    )
                kwargs["fetch_statement"](kwargs["target_month"])
                return SimpleNamespace(
                    success=True,
                    action_required=False,
                    failure=None,
                )

            worker = ReceiptWorker(
                store=store,
                inbox=inbox,
                config=WorkerRuntimeConfig(
                    owner_id="owner-1",
                    profile_root=root / "profiles",
                    download_root=root / "downloads",
                    challenge_ttl_seconds=5,
                    poll_interval_seconds=0.01,
                ),
                storage_factory=lambda: object(),
                credentials_factory=lambda _service: {
                    "login_id": "configured",
                    "password": "configured",
                },
                browser_factory=FakeBrowser,
                fetcher_factory=lambda *_args: fetcher,
                acquisition_runner=acquisition_runner,
            )
            service = WorkerService(
                store=store,
                inbox=inbox,
                worker=worker,
                owner_id="owner-1",
            )
            token = "t" * 48
            asgi = WorkerASGIApp(service=service, api_token=token)
            connection = WorkerConnection(
                base_url="https://worker.example.test",
                api_token=token,
                owner_id="owner-1",
            )
            session = ASGISession(asgi)
            iphone_client = WorkerClient(connection, session=session)

            created = iphone_client.create_job(
                target_month="2026-07",
                service_ids=["commufa"],
                idempotency_key="iphone-full-contract",
            )
            runner = threading.Thread(target=worker.run_once)
            runner.start()
            waiting = self._wait_for_state(iphone_client, created["id"], "waiting_for_challenge")

            # iPhone Chrome reload: no local state is reused, only the durable job ID.
            reloaded_client = WorkerClient(connection, session=ASGISession(asgi))
            recovered = reloaded_client.get_job(created["id"])
            self.assertEqual(waiting["challenge"]["id"], recovered["challenge"]["id"])
            self.assertEqual(6, recovered["challenge"]["input_schema"]["max_length"])

            reloaded_client.submit_challenge_response(
                job_id=created["id"],
                challenge_id=recovered["challenge"]["id"],
                response="654321",
            )
            runner.join(timeout=5)
            self.assertFalse(runner.is_alive())

            completed = reloaded_client.get_job(created["id"])
            self.assertEqual("succeeded", completed["state"])
            self.assertEqual(["commufa"], completed["completed_service_ids"])
            self.assertEqual(["654321"], fetcher.responses)

            with closing(sqlite3.connect(store.path)) as connection_db:
                durable_text = "\n".join(
                    str(value)
                    for table, column in (
                        ("batch_jobs", "result_json"),
                        ("challenges", "challenge_json"),
                        ("job_events", "payload_json"),
                    )
                    for row in connection_db.execute(f"SELECT {column} FROM {table}")
                    for value in row
                )
            self.assertNotIn("654321", durable_text)
            store.close()

    @staticmethod
    def _wait_for_state(client: WorkerClient, job_id: str, expected: str) -> dict:
        deadline = time.time() + 5
        latest: dict = {}
        while time.time() < deadline:
            latest = client.get_job(job_id)
            if latest["state"] == expected:
                return latest
            time.sleep(0.01)
        raise AssertionError(f"job did not reach {expected}: {latest}")


if __name__ == "__main__":
    unittest.main()
