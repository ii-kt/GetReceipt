from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping

from ..config import ServiceDefinition, expected_transaction_month, parse_month_key


PDF_MIME_TYPE = "application/pdf"
_RECEIPT_FILE_NAME = re.compile(
    r"^(?P<transaction_date>\d{8})_(?P<partner>.+)_(?P<amount>\d+)(?P<yen>円?)\.pdf$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StoredReceipt:
    file_id: str
    file_name: str
    web_view_link: str = ""
    mime_type: str = PDF_MIME_TYPE
    modified_time: str = ""
    size: int | None = None


class ReceiptMonthState(str, Enum):
    MISSING = "missing"
    STORED = "stored"


def find_receipt(
    files: list[Mapping[str, object]],
    service: ServiceDefinition,
    target_month: str,
) -> StoredReceipt | None:
    """Return the newest valid Drive PDF for a service usage month.

    ``target_month`` is always the usage month. The service-specific transaction
    month is derived before matching the filename date. The folder listing and
    canonical filename are the source of truth; MIME metadata is deliberately
    ignored because older PDFs may be stored as ``application/octet-stream``.
    """

    transaction_month = expected_transaction_month(service.id, target_month)
    month_prefix = _month_prefix(transaction_month)
    aliases = {
        _normalize_partner(alias)
        for alias in (service.default_partner, *service.partner_aliases)
        if _normalize_partner(alias)
    }

    matches: list[StoredReceipt] = []
    for file in files:
        file_name = str(file.get("name") or "")
        normalized_name = unicodedata.normalize("NFKC", file_name)
        if not normalized_name.lower().endswith(".pdf"):
            continue

        match = _RECEIPT_FILE_NAME.fullmatch(normalized_name)
        if not match:
            continue

        transaction_date = match.group("transaction_date")
        if transaction_date[:6] != month_prefix or not _valid_date(transaction_date):
            continue
        if not _valid_amount(match.group("amount")):
            continue
        if not match.group("yen") and not service.accepts_yenless_amount:
            continue
        if _normalize_partner(match.group("partner")) not in aliases:
            continue
        size = _optional_int(file.get("size"))
        if size is None or size <= 0:
            continue

        matches.append(
            StoredReceipt(
                file_id=str(file.get("id") or ""),
                file_name=file_name,
                web_view_link=str(file.get("webViewLink") or ""),
                mime_type=str(file.get("mimeType") or PDF_MIME_TYPE),
                modified_time=str(file.get("modifiedTime") or ""),
                size=size,
            )
        )
    return max(
        matches,
        key=lambda receipt: (receipt.modified_time, receipt.file_name, receipt.file_id),
        default=None,
    )


def receipt_month_state(
    files: list[Mapping[str, object]],
    service: ServiceDefinition,
    target_month: str,
) -> ReceiptMonthState:
    return (
        ReceiptMonthState.STORED
        if find_receipt(files, service, target_month) is not None
        else ReceiptMonthState.MISSING
    )


def _month_prefix(target_month: str) -> str:
    year, month = parse_month_key(unicodedata.normalize("NFKC", str(target_month)))
    return f"{year:04d}{month:02d}"


def _normalize_partner(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s_]+", "", text).casefold()


def _valid_amount(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
