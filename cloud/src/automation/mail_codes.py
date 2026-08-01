from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests


LOGGER = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
AccessTokenProvider = Callable[[], str]

# A provider may reuse the same code for repeated attempts, so a message that
# predates this attempt is still usable. Bound how far back that may reach.
_FALLBACK_MAX_AGE = timedelta(minutes=30)
_CLOCK_SKEW = timedelta(minutes=2)
# How long to keep looking when the provider has never mailed this mailbox at
# all: long enough for a first message still in flight, short enough that a
# code sent by SMS instead does not stall the acquisition.
_NO_SENDER_GRACE_SECONDS = 20


@dataclass(frozen=True)
class MailCodeSource:
    """Where a provider's verification code arrives and how to read it.

    ``search_terms`` only narrows what Graph returns. Trust comes from
    ``sender_domains``: a message is read only when it was really sent from
    one of the provider's own domains, so a broad search term stays safe.
    """

    search_terms: tuple[str, ...]
    sender_domains: tuple[str, ...]
    code_patterns: tuple[str, ...]
    subject_hint: str = ""

    def matches_domain(self, address: str) -> bool:
        normalized = address.strip().casefold()
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[-1]
        return any(
            domain == expected or domain.endswith("." + expected)
            for expected in (item.strip().casefold() for item in self.sender_domains)
            if expected
        )


VERIFICATION_CODE_SOURCES: dict[str, MailCodeSource] = {
    "commufa": MailCodeSource(
        search_terms=("from:news-ml@commufa.jp",),
        sender_domains=("commufa.jp",),
        subject_hint="ID を確認してください",
        code_patterns=(r"確認コード[\s:：]*([0-9]{6})",),
    ),
    # 携帯 has two sign-in routes into Web billing and each has its own code:
    #   - d-account: a security code sent to SMS or to the contact mail
    #     address, whichever destination the owner selected. Only the mail
    #     route is readable here.
    #   - Web billing ID: a one-time password NTT Finance mails from
    #     webbilling_info@ntt-finance.co.jp.
    # The exact sending address of the d-account mail is not known ahead of
    # time, so the search is by wording and the sender is checked against the
    # provider's domains rather than a guessed address.
    "mobile": MailCodeSource(
        search_terms=("セキュリティコード", "ワンタイムパスワード"),
        sender_domains=("docomo.ne.jp", "nttdocomo.co.jp", "ntt-finance.co.jp"),
        subject_hint="",
        code_patterns=(
            r"セキュリティコード[^0-9]{0,16}([0-9]{4,8})",
            r"ワンタイムパスワード[^0-9]{0,40}?([0-9]{4,8})",
        ),
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
        self._pending_fallback_id = ""
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
            fresh, recent, seen_sender = self._read_codes(source, threshold=threshold)
            if fresh:
                return fresh
            fallback = fallback or recent
            if not seen_sender:
                # This provider has never mailed this mailbox, so it is sending
                # the code somewhere else - by SMS, typically. Waiting out the
                # full timeout only delays asking the owner for it.
                deadline = min(
                    deadline, self._now() + timedelta(seconds=_NO_SENDER_GRACE_SECONDS)
                )
            if self._now() >= deadline:
                break
            self._sleep(poll_seconds)
        if fallback:
            # No new mail arrived, but this provider reissues the same code for
            # repeated attempts, so a recent message is still the live code.
            LOGGER.info("Using a recent verification mail; no newer one arrived")
            if self._pending_fallback_id:
                self._retire_message({"id": self._pending_fallback_id})
                self._pending_fallback_id = ""
            return fallback
        raise MailCodeUnavailableError(
            "メールから確認コードを読み取れませんでした。"
        )

    def _read_codes(
        self,
        source: MailCodeSource,
        *,
        threshold: datetime,
    ) -> tuple[str, str, bool]:
        """Return (fresh code, newest recent code, provider mails here at all)."""

        messages = self._search(source)
        newest_recent = ""
        seen_sender = False
        cutoff = self._now() - _FALLBACK_MAX_AGE
        for message in messages:
            if not self._sender_matches(message, source):
                continue
            seen_sender = True
            received = _parse_timestamp(message.get("receivedDateTime"))
            if received is None:
                continue
            code = self._extract_code(message, source)
            if not code:
                continue
            if received >= threshold:
                self._retire_message(message)
                return code, newest_recent, True
            if not newest_recent and received >= cutoff:
                newest_recent = code
                self._pending_fallback_id = str(message.get("id") or "")
        return "", newest_recent, seen_sender

    def _retire_message(self, message: dict[str, Any]) -> None:
        """Mark a consumed code mail as read and move it out of the inbox.

        This is best effort: it tells the owner at a glance which codes the
        app has already used, and never blocks an acquisition. It needs a
        read-write mail scope, so it is silently skipped without one.
        """

        message_id = str(message.get("id") or "")
        if not message_id:
            return
        token = ""
        headers: dict[str, str] = {}
        try:
            token = self._access_token_provider()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            path = f"{GRAPH_ROOT}/me/messages/{quote(message_id, safe='')}"
            self._session.patch(
                path,
                headers=headers,
                json={"isRead": True},
                timeout=20,
            )
            self._session.post(
                f"{path}/move",
                headers=headers,
                json={"destinationId": "archive"},
                timeout=20,
            )
        except Exception:
            # Filing is a convenience. Nothing here may cost the owner the
            # acquisition the code was read for.
            LOGGER.info("Could not file the used verification mail")
        finally:
            token = ""
            headers = {}

    def _search(self, source: MailCodeSource) -> list[dict[str, Any]]:
        """Return every candidate message across the source's search terms.

        A provider may deliver its code from more than one system, so the
        terms are queried separately and merged newest-first. Duplicates are
        dropped by message id.
        """

        merged: dict[str, dict[str, Any]] = {}
        failures = 0
        for term in source.search_terms:
            try:
                messages = self._search_once(term)
            except MailCodeUnavailableError:
                failures += 1
                continue
            for index, message in enumerate(messages):
                # Graph always returns an id; fall back to a positional key so
                # a message is never silently dropped for lacking one.
                key = str(message.get("id") or f"{term}#{index}")
                merged.setdefault(key, message)
        if failures and failures == len(source.search_terms):
            raise MailCodeUnavailableError(
                "確認コードのメールを検索できませんでした。"
            )
        return sorted(
            merged.values(),
            key=lambda item: str(item.get("receivedDateTime") or ""),
            reverse=True,
        )

    def _search_once(self, term: str) -> list[dict[str, Any]]:
        token = self._access_token_provider()
        try:
            response = self._session.get(
                f"{GRAPH_ROOT}/me/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params={
                    "$search": f'"{term}"',
                    "$top": "25",
                    "$select": "id,subject,from,receivedDateTime,body",
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
        return source.matches_domain(address)

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
        for pattern in source.code_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return ""


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


# Checked against the owner's mailbox on 2026-07-31:
#   - commufa   : sends the code by mail -> automated above.
#   - webbilling: no code mail has arrived yet, because the account signs in
#                 with a d-account and that code goes to SMS. Both readable
#                 routes are wired up in advance:
#                   * d-account with its destination set to the contact mail
#                     address (docomo sends to "SMSまたは連絡先メールアドレス"),
#                   * the Web billing ID sign-in, whose one-time password NTT
#                     Finance mails from webbilling_info@ntt-finance.co.jp.
#                 Neither can be turned on from here; the owner selects one.
#   - epos      : the three-digit code is printed on the card, never mailed,
#                 and is deliberately not stored.
#   - tokuten   : no verification step; the invoice is read through Graph.
