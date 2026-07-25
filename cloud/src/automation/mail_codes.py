from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
AccessTokenProvider = Callable[[], str]

# A provider may reuse the same code for repeated attempts, so a message that
# predates this attempt is still usable. Bound how far back that may reach.
_FALLBACK_MAX_AGE = timedelta(minutes=30)
_CLOCK_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class MailCodeSource:
    """Where a provider's verification code arrives and how to read it."""

    sender_address: str
    code_pattern: str
    subject_hint: str = ""

    def sender_domain(self) -> str:
        return self.sender_address.strip().casefold().rsplit("@", 1)[-1]


VERIFICATION_CODE_SOURCES: dict[str, MailCodeSource] = {
    "commufa": MailCodeSource(
        sender_address="news-ml@commufa.jp",
        subject_hint="ID を確認してください",
        code_pattern=r"確認コード[\s:：]*([0-9]{6})",
    ),
}


class MailCodeUnavailableError(RuntimeError):
    """Raised when no trustworthy verification code could be read."""


class MailVerificationCodeReader:
    """Read a provider's one-time code from the owner's mailbox via Graph.

    The owner already grants delegated Mail.Read for the electricity invoice.
    Reading the login code from the same mailbox removes the manual step that
    otherwise makes the acquisition impossible to finish from a phone.

    The code itself is never logged or returned in an error message.
    """

    def __init__(
        self,
        access_token_provider: AccessTokenProvider,
        *,
        session: requests.Session | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._access_token_provider = access_token_provider
        self._session = session or requests.Session()
        self._now = now
        self._sleep = sleep

    def wait_for_code(
        self,
        source: MailCodeSource,
        *,
        requested_after: datetime,
        timeout_seconds: float = 90,
        poll_seconds: float = 5,
    ) -> str:
        """Return the newest code, preferring one sent after this attempt."""

        threshold = requested_after.astimezone(timezone.utc) - _CLOCK_SKEW
        deadline = self._now() + timedelta(seconds=timeout_seconds)
        fallback = ""
        while True:
            fresh, recent = self._read_codes(source, threshold=threshold)
            if fresh:
                return fresh
            fallback = fallback or recent
            if self._now() >= deadline:
                break
            self._sleep(poll_seconds)
        if fallback:
            # No new mail arrived, but this provider reissues the same code for
            # repeated attempts, so a recent message is still the live code.
            LOGGER.info("Using a recent verification mail; no newer one arrived")
            return fallback
        raise MailCodeUnavailableError(
            "メールから確認コードを読み取れませんでした。"
        )

    def _read_codes(
        self,
        source: MailCodeSource,
        *,
        threshold: datetime,
    ) -> tuple[str, str]:
        """Return (code newer than threshold, newest recent code)."""

        messages = self._search(source)
        newest_recent = ""
        cutoff = self._now() - _FALLBACK_MAX_AGE
        for message in messages:
            if not self._sender_matches(message, source):
                continue
            received = _parse_timestamp(message.get("receivedDateTime"))
            if received is None:
                continue
            code = self._extract_code(message, source)
            if not code:
                continue
            if received >= threshold:
                return code, newest_recent
            if not newest_recent and received >= cutoff:
                newest_recent = code
        return "", newest_recent

    def _search(self, source: MailCodeSource) -> list[dict[str, Any]]:
        token = self._access_token_provider()
        try:
            response = self._session.get(
                f"{GRAPH_ROOT}/me/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params={
                    "$search": f'"from:{source.sender_address}"',
                    "$top": "10",
                    "$select": "subject,from,receivedDateTime,body",
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise MailCodeUnavailableError(
                "メールサーバーへ接続できませんでした。"
            ) from error
        finally:
            token = ""
        if response.status_code >= 400:
            raise MailCodeUnavailableError(
                "確認コードのメールを検索できませんでした。"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise MailCodeUnavailableError(
                "確認コードのメール検索結果を解釈できませんでした。"
            ) from error
        value = payload.get("value")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _sender_matches(message: dict[str, Any], source: MailCodeSource) -> bool:
        sender = message.get("from")
        address = ""
        if isinstance(sender, dict):
            email = sender.get("emailAddress")
            if isinstance(email, dict):
                address = str(email.get("address") or "")
        normalized = address.strip().casefold()
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[-1]
        expected = source.sender_domain()
        return domain == expected or domain.endswith("." + expected)

    @staticmethod
    def _extract_code(message: dict[str, Any], source: MailCodeSource) -> str:
        body = message.get("body")
        content = ""
        if isinstance(body, dict):
            content = str(body.get("content") or "")
        text = re.sub(r"<[^>]+>", " ", content)
        subject = str(message.get("subject") or "")
        if source.subject_hint and source.subject_hint not in subject:
            return ""
        match = re.search(source.code_pattern, text)
        return match.group(1) if match else ""


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
