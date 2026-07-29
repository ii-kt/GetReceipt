from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.mail_codes import (  # noqa: E402
    VERIFICATION_CODE_SOURCES,
    MailCodeUnavailableError,
    MailVerificationCodeReader,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SOURCE = VERIFICATION_CODE_SOURCES["commufa"]


def _message(
    *,
    code: str,
    minutes_ago: float,
    sender: str = "news-ml@commufa.jp",
    subject: str = "Myコミュファ で ID を確認してください",
) -> dict:
    received = NOW - timedelta(minutes=minutes_ago)
    return {
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received.isoformat().replace("+00:00", "Z"),
        "body": {
            "contentType": "text",
            "content": (
                "Myコミュファへのログインを試行しました。\n"
                "確認コード入力画面にて下記確認コードをご入力ください。\n"
                f"確認コード: {code}\n"
            ),
        },
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, messages):
        self.messages = messages
        self.requests = 0

    def get(self, url, **kwargs):
        self.requests += 1
        return _FakeResponse({"value": self.messages})


def _reader(messages, *, sleep_calls=None):
    session = _FakeSession(messages)
    reader = MailVerificationCodeReader(
        lambda: "delegated-access-token",
        session=session,
        now=lambda: NOW,
        sleep=(sleep_calls.append if sleep_calls is not None else lambda _s: None),
    )
    return reader, session


class MailVerificationCodeReaderTest(unittest.TestCase):
    def test_reads_the_code_from_a_fresh_message(self) -> None:
        reader, _ = _reader([_message(code="155493", minutes_ago=1)])

        code = reader.wait_for_code(
            SOURCE,
            requested_after=NOW - timedelta(minutes=2),
            timeout_seconds=0,
        )

        self.assertEqual("155493", code)

    def test_prefers_the_message_sent_after_the_attempt(self) -> None:
        reader, _ = _reader(
            [
                _message(code="222222", minutes_ago=0.5),
                _message(code="111111", minutes_ago=20),
            ]
        )

        code = reader.wait_for_code(
            SOURCE,
            requested_after=NOW - timedelta(minutes=1),
            timeout_seconds=0,
        )

        self.assertEqual("222222", code)

    def test_falls_back_to_a_recent_code_when_no_new_mail_arrives(self) -> None:
        """This provider reissues the same code, so a recent mail is still live."""

        reader, _ = _reader([_message(code="155493", minutes_ago=12)])

        code = reader.wait_for_code(
            SOURCE,
            requested_after=NOW,
            timeout_seconds=0,
        )

        self.assertEqual("155493", code)

    def test_ignores_a_message_older_than_the_fallback_window(self) -> None:
        reader, _ = _reader([_message(code="155493", minutes_ago=120)])

        with self.assertRaises(MailCodeUnavailableError):
            reader.wait_for_code(
                SOURCE,
                requested_after=NOW,
                timeout_seconds=0,
            )

    def test_rejects_a_lookalike_sender(self) -> None:
        reader, _ = _reader(
            [_message(code="999999", minutes_ago=1, sender="news-ml@commufa.jp.evil.example")]
        )

        with self.assertRaises(MailCodeUnavailableError):
            reader.wait_for_code(
                SOURCE,
                requested_after=NOW - timedelta(minutes=2),
                timeout_seconds=0,
            )

    def test_rejects_an_unrelated_subject_from_the_same_sender(self) -> None:
        reader, _ = _reader(
            [
                _message(
                    code="999999",
                    minutes_ago=1,
                    subject="【コミュファ】ご利用料金のお知らせ",
                )
            ]
        )

        with self.assertRaises(MailCodeUnavailableError):
            reader.wait_for_code(
                SOURCE,
                requested_after=NOW - timedelta(minutes=2),
                timeout_seconds=0,
            )

    def test_polls_until_the_timeout_before_giving_up(self) -> None:
        sleeps: list[float] = []
        reader, session = _reader([], sleep_calls=sleeps)
        clock = [NOW]
        reader._now = lambda: clock[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] = clock[0] + timedelta(seconds=seconds)

        reader._sleep = sleep

        with self.assertRaises(MailCodeUnavailableError):
            reader.wait_for_code(
                SOURCE,
                requested_after=NOW,
                timeout_seconds=12,
                poll_seconds=5,
            )

        self.assertGreater(session.requests, 1)



class UsedCodeMailIsFiledTest(unittest.TestCase):
    """A consumed code mail is marked read and archived.

    Without this the inbox fills with codes and the owner cannot tell which
    one the app is currently working with.
    """

    class _Session:
        def __init__(self, messages):
            self.messages = messages
            self.patched: list[tuple[str, dict]] = []
            self.moved: list[tuple[str, dict]] = []

        def get(self, url, **kwargs):
            return _FakeResponse({"value": self.messages})

        def patch(self, url, **kwargs):
            self.patched.append((url, kwargs.get("json") or {}))
            return _FakeResponse({})

        def post(self, url, **kwargs):
            self.moved.append((url, kwargs.get("json") or {}))
            return _FakeResponse({})

    def _reader(self, messages):
        session = self._Session(messages)
        reader = MailVerificationCodeReader(
            lambda: "delegated-access-token",
            session=session,
            now=lambda: NOW,
            sleep=lambda _s: None,
        )
        return reader, session

    def test_fresh_code_mail_is_marked_read_and_archived(self) -> None:
        message = _message(code="155493", minutes_ago=1)
        message["id"] = "AAMkAD-message-id"
        reader, session = self._reader([message])

        reader.wait_for_code(
            SOURCE,
            requested_after=NOW - timedelta(minutes=2),
            timeout_seconds=0,
        )

        self.assertEqual(1, len(session.patched))
        self.assertEqual({"isRead": True}, session.patched[0][1])
        self.assertEqual(1, len(session.moved))
        self.assertEqual({"destinationId": "archive"}, session.moved[0][1])
        self.assertTrue(session.moved[0][0].endswith("/move"))

    def test_filing_failure_never_breaks_the_acquisition(self) -> None:
        import requests as requests_module

        message = _message(code="155493", minutes_ago=1)
        message["id"] = "AAMkAD-message-id"
        reader, session = self._reader([message])

        def boom(url, **kwargs):
            raise requests_module.RequestException("no write scope")

        session.patch = boom  # type: ignore[method-assign]

        code = reader.wait_for_code(
            SOURCE,
            requested_after=NOW - timedelta(minutes=2),
            timeout_seconds=0,
        )

        self.assertEqual("155493", code)


if __name__ == "__main__":
    unittest.main()
