from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.billing_notices import (  # noqa: E402
    BILLING_NOTICE_SOURCES,
    BillingNoticeFiler,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, messages):
        self.messages = messages
        self.patched: list[tuple[str, dict]] = []
        self.moved: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        return _Response({"value": self.messages})

    def patch(self, url, **kwargs):
        self.patched.append((url, kwargs.get("json") or {}))
        return _Response({})

    def post(self, url, **kwargs):
        self.moved.append((url, kwargs.get("json") or {}))
        return _Response({})


def message(
    *,
    message_id: str,
    subject: str,
    sender: str,
    body: str = "",
) -> dict:
    return {
        "id": message_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-07-14T00:00:00Z",
        "body": {"contentType": "text", "content": body},
    }


def filer(messages):
    session = _Session(messages)
    return BillingNoticeFiler(lambda: "delegated-token", session=session), session


COMMUFA_JULY = message(
    message_id="commufa-2026-07",
    subject="【コミュファ】7月ご利用料金のお知らせ",
    sender="seikyugaku@commufa.jp",
    body="2026年7月分（6月利用分）のご利用料金が確定しました。",
)
COMMUFA_JUNE = message(
    message_id="commufa-2026-06",
    subject="【コミュファ】6月ご利用料金のお知らせ",
    sender="seikyugaku@commufa.jp",
    body="2026年6月分（5月利用分）のご利用料金が確定しました。",
)
MOBILE_JULY = message(
    message_id="mobile-2026-07",
    subject="請求料金のご案内（NTTファイナンス）",
    sender="webbilling_info@ntt-finance.co.jp",
    body="2026年7月ご請求分は 4,882 円です。",
)


class BillingNoticeFilerTest(unittest.TestCase):
    """A saved month's notice is filed; nothing else is touched."""

    def test_the_notice_for_the_saved_month_is_read_and_archived(self) -> None:
        subject, session = filer([COMMUFA_JUNE, COMMUFA_JULY])

        result = subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertTrue(result.filed)
        self.assertEqual(1, len(session.patched))
        self.assertIn("commufa-2026-07", session.patched[0][0])
        self.assertEqual({"isRead": True}, session.patched[0][1])
        self.assertEqual({"destinationId": "archive"}, session.moved[0][1])

    def test_another_month_is_left_alone(self) -> None:
        subject, session = filer([COMMUFA_JUNE])

        result = subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertFalse(result.filed)
        self.assertEqual([], session.patched)
        self.assertEqual([], session.moved)

    def test_the_month_can_come_from_the_body_when_the_subject_omits_it(self) -> None:
        subject, session = filer([MOBILE_JULY])

        result = subject.file_for_month("mobile", transaction_month="2026-07")

        self.assertTrue(result.filed)
        self.assertIn("mobile-2026-07", session.patched[0][0])

    def test_a_lookalike_sender_is_refused(self) -> None:
        impostor = message(
            message_id="fake",
            subject="【コミュファ】7月ご利用料金のお知らせ",
            sender="billing@commufa.jp.evil.example",
        )
        subject, session = filer([impostor])

        result = subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertFalse(result.filed)
        self.assertEqual([], session.moved)

    def test_an_unrelated_mail_from_the_same_provider_is_refused(self) -> None:
        unrelated = message(
            message_id="campaign",
            subject="【コミュファ】7月のキャンペーンのお知らせ",
            sender="seikyugaku@commufa.jp",
        )
        subject, session = filer([unrelated])

        result = subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertFalse(result.filed)
        self.assertEqual([], session.moved)

    def test_electricity_files_the_message_the_statement_came_from(self) -> None:
        """No month matching at all: the exact message is already known."""

        subject, session = filer([])

        result = subject.file_for_month(
            "tokuten", transaction_month="2026-07", message_id="tokuten-mail-1"
        )

        self.assertTrue(result.filed)
        self.assertEqual("source_message", result.reason)
        self.assertIn("tokuten-mail-1", session.patched[0][0])

    def test_a_provider_without_a_notice_mail_is_a_no_op(self) -> None:
        """Epos never mails a monthly statement notice."""

        self.assertNotIn("epos", BILLING_NOTICE_SOURCES)
        subject, session = filer([COMMUFA_JULY])

        result = subject.file_for_month("epos", transaction_month="2026-06")

        self.assertFalse(result.filed)
        self.assertEqual("no_notice_mail", result.reason)
        self.assertEqual([], session.moved)

    def test_a_mailbox_failure_never_raises(self) -> None:
        class _Broken:
            def get(self, *args, **kwargs):
                raise RuntimeError("mailbox unreachable")

        subject = BillingNoticeFiler(lambda: "token", session=_Broken())

        result = subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertFalse(result.filed)
        self.assertEqual("error", result.reason)

    def test_nothing_is_ever_deleted(self) -> None:
        subject, session = filer([COMMUFA_JULY])

        subject.file_for_month("commufa", transaction_month="2026-07")

        self.assertFalse(hasattr(session, "deleted"))
        for _, payload in session.moved:
            self.assertEqual("archive", payload["destinationId"])


if __name__ == "__main__":
    unittest.main()
