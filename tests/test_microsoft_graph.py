from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.automation.epos import AcquisitionError  # noqa: E402
from src.automation.microsoft_graph import (  # noqa: E402
    MAX_GRAPH_ATTACHMENT_BYTES,
    TokutenGraphFetcher,
)


class FakeResponse:
    def __init__(self, status_code=200, *, payload=None, content=b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"content-length": str(len(content))} if content else {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=64 * 1024):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self):
        return None


class FakeSession:
    def __init__(
        self,
        pdf: bytes,
        *,
        sender_address: str = "notice@besender-s.jp",
        sender_name: str = "トクテンでんき 総合サポートセンター",
        attachment_size: int | None = None,
        authentication_result: str = (
            "spf=pass; dkim=pass; "
            "dmarc=pass action=none header.from=besender-s.jp"
        ),
    ) -> None:
        self.pdf = pdf
        self.sender_address = sender_address
        self.sender_name = sender_name
        self.attachment_size = (
            len(pdf) if attachment_size is None else attachment_size
        )
        self.authentication_result = authentication_result
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/me/messages"):
            return FakeResponse(
                payload={
                    "value": [
                        {
                            "id": "message-id",
                            "subject": "【トクテンでんき】2026年8月 請求額確定のお知らせ",
                            "from": {
                                "emailAddress": {
                                    "address": self.sender_address,
                                    "name": self.sender_name,
                                }
                            },
                            "receivedDateTime": "2026-08-05T01:00:00Z",
                            "hasAttachments": True,
                        }
                    ]
                }
            )
        if url.endswith("/me/messages/message-id"):
            return FakeResponse(
                payload={
                    "internetMessageHeaders": [
                        {
                            "name": "Authentication-Results",
                            "value": self.authentication_result,
                        }
                    ]
                }
            )
        if url.endswith("/attachments"):
            return FakeResponse(
                payload={
                    "value": [
                        {
                            "id": "attachment-id",
                            "name": "【トクテンでんき】2026年8月請求書.pdf",
                            "contentType": "application/pdf",
                            "size": self.attachment_size,
                            "isInline": False,
                        }
                    ]
                }
            )
        if url.endswith("/$value"):
            return FakeResponse(content=self.pdf)
        raise AssertionError(url)


class MicrosoftGraphTest(unittest.TestCase):
    def test_mail_read_fetches_only_matching_monthly_pdf(self) -> None:
        pdf = (
            "%PDF-1.7\nフラットエナジー株式会社 トクテンでんき "
            "2026年8月 ご請求額 10,003円\n%%EOF"
        ).encode()
        session = FakeSession(pdf)
        fetcher = TokutenGraphFetcher(
            lambda: "delegated-access-token-value",
            session=session,
        )
        statement = fetcher.fetch_pdf("2026-07")
        self.assertEqual(pdf, statement.content)
        self.assertEqual(
            "【トクテンでんき】2026年8月請求書.pdf",
            statement.original_file_name,
        )
        self.assertEqual(4, len(session.calls))
        self.assertTrue(
            all(
                call[1]["headers"]["Authorization"]
                == "Bearer delegated-access-token-value"
                for call in session.calls
            )
        )

    def test_sender_display_name_or_subject_cannot_spoof_allowed_domain(self) -> None:
        pdf = (
            "%PDF-1.7\nフラットエナジー株式会社 トクテンでんき "
            "2026年8月 ご請求額 10,003円\n%%EOF"
        ).encode()
        session = FakeSession(
            pdf,
            sender_address="attacker@example.net",
            sender_name="notice@besender-s.jp トクテンでんき",
        )
        fetcher = TokutenGraphFetcher(
            lambda: "delegated-access-token-value",
            session=session,
        )

        with self.assertRaises(AcquisitionError) as raised:
            fetcher.fetch_pdf("2026-07")

        self.assertEqual(
            "TOKUTEN_GRAPH_ATTACHMENT_NOT_FOUND",
            raised.exception.code,
        )
        self.assertEqual(1, len(session.calls))

    def test_forged_from_domain_is_rejected_when_dmarc_fails(self) -> None:
        pdf = (
            "%PDF-1.7\nフラットエナジー株式会社 トクテンでんき "
            "2026年8月 ご請求額 10,003円\n%%EOF"
        ).encode()
        session = FakeSession(
            pdf,
            authentication_result=(
                "spf=fail; dkim=fail; "
                "dmarc=fail action=quarantine header.from=besender-s.jp"
            ),
        )
        fetcher = TokutenGraphFetcher(
            lambda: "delegated-access-token-value",
            session=session,
        )

        with self.assertRaises(AcquisitionError) as raised:
            fetcher.fetch_pdf("2026-07")

        self.assertEqual(
            "TOKUTEN_GRAPH_ATTACHMENT_NOT_FOUND",
            raised.exception.code,
        )
        self.assertEqual(2, len(session.calls))

    def test_oversized_attachment_is_rejected_before_download(self) -> None:
        pdf = b"%PDF-1.7\nsmall metadata fixture\n%%EOF"
        session = FakeSession(
            pdf,
            attachment_size=MAX_GRAPH_ATTACHMENT_BYTES + 1,
        )
        fetcher = TokutenGraphFetcher(
            lambda: "delegated-access-token-value",
            session=session,
        )

        with self.assertRaises(AcquisitionError):
            fetcher.fetch_pdf("2026-07")

        self.assertEqual(3, len(session.calls))


if __name__ == "__main__":
    unittest.main()
