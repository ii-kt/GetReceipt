from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..config import expected_transaction_month, parse_month_key, service_by_id
from ..domain.document_metadata import extract_pdf_text


@dataclass(frozen=True)
class StatementValidation:
    partner_found: bool
    month_found: bool

    @property
    def valid(self) -> bool:
        return self.partner_found and self.month_found


_PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "epos": ("エポス", "epos"),
    "commufa": ("コミュファ", "中部テレコミュニケーション"),
    "tokuten": ("トクテンでんき", "フラットエナジー", "flatenergy"),
    "mobile": ("nttファイナンス", "nttドコモ", "webビリング", "docomo"),
}


def inspect_acquired_statement(
    *,
    service_id: str,
    target_month: str,
    content: bytes,
    metadata_text: str = "",
    original_file_name: str = "",
) -> StatementValidation:
    """Confirm that an automatically fetched PDF belongs to the requested bill.

    The page text and attachment name are included because some official PDFs
    are image-based and have no extractable text. Both the provider identity
    and an exact service-relevant month must still be present.
    """

    service = service_by_id(service_id)
    parse_month_key(target_month)
    text = _normalize(
        " ".join(
            (
                metadata_text,
                original_file_name,
                extract_pdf_text(content),
            )
        )
    )
    provider_hints = {
        _normalize(value)
        for value in (
            service.default_partner,
            *service.partner_aliases,
            *_PROVIDER_HINTS.get(service_id, ()),
        )
        if value
    }
    partner_found = any(hint in text for hint in provider_hints)
    month_found = any(
        _contains_month(text, month_key)
        for month_key in _relevant_months(service_id, target_month)
    )
    return StatementValidation(
        partner_found=partner_found,
        month_found=month_found,
    )


def _relevant_months(service_id: str, target_month: str) -> tuple[str, ...]:
    transaction_month = expected_transaction_month(service_id, target_month)
    if service_id == "epos":
        # EPOS statements are selected by payment month.
        return (transaction_month,)
    if transaction_month == target_month:
        return (target_month,)
    # Utilities can print either the usage month or the following billing month.
    return (target_month, transaction_month)


def _contains_month(text: str, month_key: str) -> bool:
    year, month = parse_month_key(month_key)
    patterns = (
        f"{year}年{month}月",
        f"{year}年{month:02d}月",
        f"{year}/{month}",
        f"{year}/{month:02d}",
        f"{year}-{month:02d}",
        f"{year}.{month:02d}",
        f"{year}{month:02d}",
    )
    return any(_normalize(pattern) in text for pattern in patterns)


def _normalize(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).lower(),
    )
