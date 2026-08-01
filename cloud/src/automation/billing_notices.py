"""File a provider's "your bill is ready" mail once its receipt is saved.

Every provider except Epos announces the month's statement by mail, and those
notices pile up in the inbox with no way to tell which ones have already been
dealt with. Once a month's PDF is in Drive its notice has served its purpose,
so it is marked read and moved to the archive - the same treatment the used
verification codes already get.

Nothing is ever deleted, and a notice is only touched when it can be tied to
the exact month that was just saved. When the month cannot be established the
mail is left alone: a tidy inbox is never worth filing the wrong message.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests


LOGGER = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
ARCHIVE_FOLDER = "archive"


@dataclass(frozen=True)
class BillingNoticeSource:
    """How to recognise one provider's monthly billing notice."""

    search_term: str
    sender_domains: tuple[str, ...]
    subject_hints: tuple[str, ...] = ()
    # Templates are formatted with the transaction year and month. A message
    # qualifies when its subject or body carries one of them.
    subject_month_templates: tuple[str, ...] = ()
    body_month_templates: tuple[str, ...] = ()

    def matches_domain(self, address: str) -> bool:
        normalized = str(address or "").strip().casefold()
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[-1]
        return any(
            domain == expected or domain.endswith("." + expected)
            for expected in (item.strip().casefold() for item in self.sender_domains)
            if expected
        )


BILLING_NOTICE_SOURCES: dict[str, BillingNoticeSource] = {
    # 「【コミュファ】7月ご利用料金のお知らせ」 - the month in the subject is
    # the billing month, which is the month on the receipt's own filename.
    "commufa": BillingNoticeSource(
        search_term="ご利用料金のお知らせ",
        sender_domains=("commufa.jp",),
        subject_hints=("ご利用料金のお知らせ",),
        subject_month_templates=("{month}月",),
    ),
    # 「請求料金のご案内（NTTファイナンス）」 - the subject never names a
    # month, but the body says 「2026年7月ご請求分は…」.
    "mobile": BillingNoticeSource(
        search_term="請求料金のご案内",
        sender_domains=("ntt-finance.co.jp",),
        subject_hints=("請求料金のご案内",),
        body_month_templates=("{year}年{month}月ご請求分", "{year}年{month}月分"),
    ),
    # Electricity is filed by message id instead: its statement is the mail's
    # own attachment, so the exact message is already known.
}


@dataclass
class NoticeFilingResult:
    filed: bool = False
    reason: str = ""
    logs: tuple[str, ...] = field(default_factory=tuple)


class BillingNoticeFiler:
    """Mark a saved month's billing notice as read and archive it."""

    def __init__(
        self,
        access_token_provider: Any,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self._access_token_provider = access_token_provider
        self._session = session or requests.Session()

    def file_for_month(
        self,
        service_id: str,
        *,
        transaction_month: str,
        message_id: str = "",
    ) -> NoticeFilingResult:
        """File the notice for one saved month. Never raises."""

        try:
            if message_id:
                # The statement came from this very message.
                self._retire(message_id)
                return NoticeFilingResult(filed=True, reason="source_message")
            source = BILLING_NOTICE_SOURCES.get(service_id)
            if source is None:
                return NoticeFilingResult(reason="no_notice_mail")
            found = self._find(source, transaction_month=transaction_month)
            if not found:
                return NoticeFilingResult(reason="not_found")
            self._retire(found)
            return NoticeFilingResult(filed=True, reason="matched_month")
        except Exception:
            # Tidying the inbox must never cost an acquisition that worked.
            LOGGER.info("Billing notice was not filed for %s", service_id)
            return NoticeFilingResult(reason="error")

    def _find(self, source: BillingNoticeSource, *, transaction_month: str) -> str:
        year, month = _parse_month(transaction_month)
        if not year:
            return ""
        for message in self._search(source.search_term):
            sender = message.get("from")
            address = ""
            if isinstance(sender, dict):
                email = sender.get("emailAddress")
                if isinstance(email, dict):
                    address = str(email.get("address") or "")
            if not source.matches_domain(address):
                continue
            subject = str(message.get("subject") or "")
            if source.subject_hints and not any(
                hint in subject for hint in source.subject_hints
            ):
                continue
            if _mentions_month(subject, source.subject_month_templates, year, month):
                return str(message.get("id") or "")
            body = message.get("body")
            content = str(body.get("content") or "") if isinstance(body, dict) else ""
            text = re.sub(r"<[^>]+>", " ", content)
            if _mentions_month(text, source.body_month_templates, year, month):
                return str(message.get("id") or "")
        return ""

    def _search(self, term: str) -> list[dict[str, Any]]:
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
        finally:
            token = ""
        if response.status_code >= 400:
            return []
        value = response.json().get("value")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _retire(self, message_id: str) -> None:
        token = self._access_token_provider()
        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            path = f"{GRAPH_ROOT}/me/messages/{quote(str(message_id), safe='')}"
            self._session.patch(path, headers=headers, json={"isRead": True}, timeout=20)
            self._session.post(
                f"{path}/move",
                headers=headers,
                json={"destinationId": ARCHIVE_FOLDER},
                timeout=20,
            )
        finally:
            token = ""


def _parse_month(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(value or "").strip())
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _mentions_month(
    text: str,
    templates: tuple[str, ...],
    year: int,
    month: int,
) -> bool:
    if not templates or not text:
        return False
    haystack = str(text).replace("　", " ")
    for template in templates:
        for rendered in {
            template.format(year=year, month=month),
            template.format(year=year, month=f"{month:02d}"),
        }:
            if rendered in haystack:
                return True
    return False
