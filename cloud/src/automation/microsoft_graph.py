from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from pathlib import PurePath
from typing import Any
from urllib.parse import quote

import requests

from .epos import AcquisitionError, FetchedStatement
from .official_sites import (
    SERVICE_AUTOMATION_CONFIGS,
    build_tokuten_search_query,
)
from ..config import expected_transaction_month, parse_month_key, service_by_id
from ..domain.document_metadata import extract_pdf_text


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_GRAPH_ATTACHMENT_BYTES = 20 * 1024 * 1024
AccessTokenProvider = Callable[[], str]


class TokutenGraphFetcher:
    """Read the monthly Tokuten attachment with delegated Graph Mail.Read."""

    def __init__(
        self,
        access_token_provider: AccessTokenProvider,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.access_token_provider = access_token_provider
        self.session = session or requests.Session()
        # The message the statement actually came from. Filing that exact one
        # afterwards needs no month guessing at all.
        self.source_message_id = ""
        self.service = service_by_id("tokuten")
        self.config = SERVICE_AUTOMATION_CONFIGS["tokuten"]

    def fetch_pdf(self, target_month: str) -> FetchedStatement:
        query = build_tokuten_search_query(target_month, self.config)
        token = self.access_token_provider()
        try:
            messages = self._get_json(
                "/me/messages",
                token=token,
                params={
                    "$search": f'"{query}"',
                    "$top": "25",
                    "$select": "id,subject,from,receivedDateTime,hasAttachments",
                },
            ).get("value")
            if not isinstance(messages, list):
                messages = []
            candidates = sorted(
                (item for item in messages if isinstance(item, dict)),
                key=lambda item: str(item.get("receivedDateTime") or ""),
                reverse=True,
            )
            for message in candidates:
                if not self._message_matches(message, target_month):
                    continue
                message_id = quote(str(message.get("id") or ""), safe="")
                if not message_id or not bool(message.get("hasAttachments")):
                    continue
                header_payload = self._get_json(
                    f"/me/messages/{message_id}",
                    token=token,
                    params={"$select": "internetMessageHeaders"},
                )
                if not self._sender_authentication_passes(
                    header_payload.get("internetMessageHeaders")
                ):
                    continue
                attachments = self._get_json(
                    f"/me/messages/{message_id}/attachments",
                    token=token,
                    params={"$select": "id,name,contentType,size,isInline"},
                ).get("value")
                if not isinstance(attachments, list):
                    continue
                for attachment in attachments:
                    if not isinstance(attachment, dict) or bool(attachment.get("isInline")):
                        continue
                    file_name = str(attachment.get("name") or "")
                    content_type = str(attachment.get("contentType") or "").lower()
                    try:
                        attachment_size = int(attachment.get("size") or 0)
                    except (TypeError, ValueError):
                        attachment_size = 0
                    if not (
                        file_name.lower().endswith(".pdf")
                        or content_type == "application/pdf"
                    ):
                        continue
                    if (
                        attachment_size <= 0
                        or attachment_size > MAX_GRAPH_ATTACHMENT_BYTES
                    ):
                        continue
                    attachment_id = quote(str(attachment.get("id") or ""), safe="")
                    if not attachment_id:
                        continue
                    content = self._get_bytes(
                        (
                            f"/me/messages/{message_id}/attachments/"
                            f"{attachment_id}/$value"
                        ),
                        token=token,
                    )
                    if self._statement_matches(
                        content,
                        target_month=target_month,
                        subject=str(message.get("subject") or ""),
                        file_name=file_name,
                    ):
                        self.source_message_id = str(message_id)
                        return FetchedStatement(
                            content=content,
                            source_url="https://outlook.live.com/mail/0/",
                            original_file_name=PurePath(file_name).name or "tokuten.pdf",
                            metadata_text=" ".join(
                                (
                                    str(message.get("subject") or ""),
                                    file_name,
                                    extract_pdf_text(content),
                                )
                            ),
                            logs=("Microsoft Graph Mail.Readで添付PDFを取得しました。",),
                        )
        finally:
            token = ""
        # Distinguish "the invoice has not been issued yet" from "the mailbox
        # cannot be read": pointing at the connection sends the owner to fix
        # something that is not broken.
        expected = expected_transaction_month("tokuten", target_month)
        year, month = parse_month_key(expected)
        raise AcquisitionError(
            f"トクテンでんきの{year}年{month}月分の請求メールがまだ見つかりません。",
            code="TOKUTEN_GRAPH_ATTACHMENT_NOT_FOUND",
            advice=(
                f"この利用月の領収書は{year}年{month}月分の請求確定メールから取得します。"
                "請求が確定して添付PDFが届いてから再実行してください。"
            ),
        )

    def _message_matches(self, message: dict[str, Any], target_month: str) -> bool:
        sender = message.get("from")
        address = ""
        if isinstance(sender, dict):
            email = sender.get("emailAddress")
            if isinstance(email, dict):
                address = str(email.get("address") or "")
        subject = str(message.get("subject") or "")
        normalized_address = address.strip().casefold()
        address_domain = normalized_address.rsplit("@", 1)[-1]
        allowed_domains = {
            hint.strip().casefold().lstrip("@")
            for hint in self.config.sender_hints
            if re.fullmatch(
                r"@?[a-z0-9.-]+\.[a-z]{2,}",
                hint.strip().casefold(),
            )
        }
        sender_match = bool(normalized_address and "@" in normalized_address) and any(
            address_domain == domain or address_domain.endswith("." + domain)
            for domain in allowed_domains
        )
        subject_text = _normalize(subject)
        subject_match = any(
            _normalize(hint) in subject_text
            for hint in self.config.subject_hints
        )
        expected_month = expected_transaction_month("tokuten", target_month)
        month_match = _contains_month(subject_text, expected_month) or str(
            message.get("receivedDateTime") or ""
        ).startswith(expected_month)
        return sender_match and subject_match and month_match

    def _sender_authentication_passes(self, raw_headers: Any) -> bool:
        if not isinstance(raw_headers, list):
            return False
        allowed_domains = {
            hint.strip().casefold().lstrip("@")
            for hint in self.config.sender_hints
            if re.fullmatch(
                r"@?[a-z0-9.-]+\.[a-z]{2,}",
                hint.strip().casefold(),
            )
        }
        authentication_results: list[str] = []
        for item in raw_headers:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().casefold()
            if name not in {"authentication-results", "arc-authentication-results"}:
                continue
            value = re.sub(
                r"\s+",
                " ",
                str(item.get("value") or "").strip().casefold(),
            )
            if value:
                authentication_results.append(value)
        if not authentication_results:
            return False
        combined = " ; ".join(authentication_results)
        if re.search(r"\bdmarc\s*=\s*(?:fail|temperror|permerror)\b", combined):
            return False
        for value in authentication_results:
            if not re.search(r"\bdmarc\s*=\s*pass\b", value):
                continue
            match = re.search(
                r"\bheader\.from\s*=\s*<?@?([a-z0-9.-]+\.[a-z]{2,})>?",
                value,
            )
            authenticated_domain = match.group(1).rstrip(".") if match else ""
            if any(
                authenticated_domain == domain
                or authenticated_domain.endswith("." + domain)
                for domain in allowed_domains
            ):
                return True
        return False

    @staticmethod
    def _statement_matches(
        content: bytes,
        *,
        target_month: str,
        subject: str,
        file_name: str,
    ) -> bool:
        if not content.startswith(b"%PDF"):
            return False
        text = _normalize(" ".join((subject, file_name, extract_pdf_text(content))))
        partner_match = any(
            value in text
            for value in ("トクテン", "フラットエナジー", "flatenergy")
        )
        month_match = _contains_month(
            text,
            expected_transaction_month("tokuten", target_month),
        )
        return partner_match and month_match

    def _get_json(
        self,
        path: str,
        *,
        token: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        response = self._request(path, token=token, params=params)
        try:
            payload = response.json()
        except ValueError as error:
            raise AcquisitionError(
                "Microsoft Graphから有効な応答を受け取れませんでした。",
                code="MICROSOFT_GRAPH_INVALID_RESPONSE",
            ) from error
        if not isinstance(payload, dict):
            raise AcquisitionError(
                "Microsoft Graphの応答形式を確認できませんでした。",
                code="MICROSOFT_GRAPH_INVALID_RESPONSE",
            )
        return payload

    def _get_bytes(self, path: str, *, token: str) -> bytes:
        response = self._request(
            path,
            token=token,
            params={},
            stream=True,
        )
        try:
            content_length = str(response.headers.get("content-length") or "")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = -1
                if declared_size < 0 or declared_size > MAX_GRAPH_ATTACHMENT_BYTES:
                    raise AcquisitionError(
                        "Microsoftメールの添付PDFが大きすぎます。",
                        code="TOKUTEN_GRAPH_ATTACHMENT_TOO_LARGE",
                    )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                value = bytes(chunk or b"")
                if not value:
                    continue
                total += len(value)
                if total > MAX_GRAPH_ATTACHMENT_BYTES:
                    raise AcquisitionError(
                        "Microsoftメールの添付PDFが大きすぎます。",
                        code="TOKUTEN_GRAPH_ATTACHMENT_TOO_LARGE",
                    )
                chunks.append(value)
            return b"".join(chunks)
        finally:
            response.close()

    def _request(
        self,
        path: str,
        *,
        token: str,
        params: dict[str, str],
        stream: bool = False,
    ) -> requests.Response:
        try:
            response = self.session.get(
                f"{GRAPH_ROOT}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, application/pdf",
                    "ConsistencyLevel": "eventual",
                },
                params=params,
                timeout=30,
                stream=stream,
            )
        except requests.RequestException as error:
            raise AcquisitionError(
                "Microsoft Graphへ接続できませんでした。",
                code="MICROSOFT_GRAPH_UNREACHABLE",
            ) from error
        if response.status_code == 401:
            raise AcquisitionError(
                "Microsoftメールの接続が失効しました。",
                code="MICROSOFT_OAUTH_RECONNECT_REQUIRED",
                advice="iPhoneからMicrosoftメールを再接続してください。",
            )
        if response.status_code >= 400:
            raise AcquisitionError(
                "Microsoft Graphがメール取得要求を拒否しました。",
                code=f"MICROSOFT_GRAPH_HTTP_{response.status_code}",
            )
        return response


def _normalize(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).lower(),
    )


def _contains_month(text: str, month_key: str) -> bool:
    year, month = parse_month_key(month_key)
    normalized = _normalize(text)
    return any(
        _normalize(pattern) in normalized
        for pattern in (
            f"{year}年{month}月",
            f"{year}年{month:02d}月",
            f"{year}/{month}",
            f"{year}/{month:02d}",
            f"{year}-{month:02d}",
            f"{year}{month:02d}",
        )
    )
