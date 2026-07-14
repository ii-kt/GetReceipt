from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable, Mapping

from ..config import SERVICES, usage_month_for_transaction
from .drive_status import find_receipt


class ReceiptCurrency(str, Enum):
    JPY = "JPY"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"

    @property
    def symbol(self) -> str:
        return {
            ReceiptCurrency.JPY: "円",
            ReceiptCurrency.USD: "$",
            ReceiptCurrency.EUR: "€",
            ReceiptCurrency.GBP: "£",
        }[self]


@dataclass(frozen=True)
class OneOffReceipt:
    file_id: str
    file_name: str
    transaction_date: date
    partner_name: str
    amount_label: str
    currency: ReceiptCurrency
    charged_amount: Decimal
    refund_amount: Decimal
    web_view_link: str = ""
    modified_time: str = ""
    size: int = 0

    @property
    def net_amount(self) -> Decimal:
        return self.charged_amount - self.refund_amount

    @property
    def is_refund(self) -> bool:
        return self.refund_amount > 0

    @property
    def transaction_month(self) -> str:
        return self.transaction_date.strftime("%Y-%m")


@dataclass(frozen=True)
class ReviewFile:
    file_id: str
    file_name: str
    reason: str
    web_view_link: str = ""
    modified_time: str = ""
    size: int | None = None


@dataclass(frozen=True)
class ReceiptArchive:
    receipts: tuple[OneOffReceipt, ...]
    review_files: tuple[ReviewFile, ...]
    ignored_non_pdf_count: int = 0

    @property
    def one_off_receipts(self) -> tuple[OneOffReceipt, ...]:
        return self.receipts


@dataclass(frozen=True)
class _Amount:
    currency: ReceiptCurrency
    charged: Decimal
    refunded: Decimal


@dataclass(frozen=True)
class _FilenameParts:
    transaction_date: date
    partner_name: str
    normalized_partner: str
    amount_label: str
    normalized_amount: str


_MINUS_SIGNS = re.compile(r"[‐‑‒–—−]")
_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]{1,2})?"
_INTEGER = r"[0-9][0-9,]*"
_CURRENCY_PATTERNS = (
    (
        ReceiptCurrency.JPY,
        re.compile(rf"^(?P<charged>{_INTEGER})円(?:-(?P<refund>{_INTEGER})円)?$"),
    ),
    (
        ReceiptCurrency.JPY,
        re.compile(rf"^¥(?P<charged>{_INTEGER})(?:-¥(?P<refund>{_INTEGER}))?$"),
    ),
    (
        ReceiptCurrency.USD,
        re.compile(rf"^\$(?P<charged>{_NUMBER})(?:-\$(?P<refund>{_NUMBER}))?$"),
    ),
    (
        ReceiptCurrency.EUR,
        re.compile(rf"^€(?P<charged>{_NUMBER})(?:-€(?P<refund>{_NUMBER}))?$"),
    ),
    (
        ReceiptCurrency.EUR,
        re.compile(rf"^(?P<charged>{_NUMBER})€(?:-(?P<refund>{_NUMBER})€)?$"),
    ),
    (
        ReceiptCurrency.GBP,
        re.compile(rf"^£(?P<charged>{_NUMBER})(?:-£(?P<refund>{_NUMBER}))?$"),
    ),
    (
        ReceiptCurrency.GBP,
        re.compile(rf"^(?P<charged>{_NUMBER})£(?:-(?P<refund>{_NUMBER})£)?$"),
    ),
)

_REVIEW_INVALID_SIZE = "PDFのファイルサイズが0または取得できません"
_REVIEW_INVALID_NAME = "領収書ファイル名を解析できません"
_REVIEW_RECURRING_MISMATCH = "月次サービスの領収書名が取得済み条件と一致しません"


def build_receipt_archive(files: Iterable[Mapping[str, object]]) -> ReceiptArchive:
    """Classify Drive PDFs without weakening the recurring-service contract.

    A file satisfying the existing recurring matcher is excluded from the
    one-off archive. This is evaluated one file at a time so duplicate and
    future-month recurring PDFs are all excluded, not just the newest file for
    a currently selectable month.
    """

    receipts: list[OneOffReceipt] = []
    review_files: list[ReviewFile] = []
    ignored_non_pdf_count = 0

    for file in files:
        file_name = str(file.get("name") or "")
        if not _is_pdf_name(file_name):
            ignored_non_pdf_count += 1
            continue

        size = _optional_int(file.get("size"))
        if size is None or size <= 0:
            review_files.append(_review_file(file, _REVIEW_INVALID_SIZE, size=size))
            continue

        if _matching_recurring_service_id(file, file_name):
            continue

        filename_parts = _parse_filename(file_name)
        known_service_id = _known_service_id(
            filename_parts.normalized_partner
            if filename_parts is not None
            else _partner_hint(file_name)
        )
        if filename_parts is None:
            review_files.append(
                _review_file(
                    file,
                    _REVIEW_RECURRING_MISMATCH if known_service_id else _REVIEW_INVALID_NAME,
                    size=size,
                )
            )
            continue

        if known_service_id:
            review_files.append(_review_file(file, _REVIEW_RECURRING_MISMATCH, size=size))
            continue

        amount = _parse_amount(filename_parts.normalized_amount)
        if amount is None:
            review_files.append(_review_file(file, _REVIEW_INVALID_NAME, size=size))
            continue

        receipts.append(
            OneOffReceipt(
                file_id=str(file.get("id") or ""),
                file_name=file_name,
                transaction_date=filename_parts.transaction_date,
                partner_name=filename_parts.partner_name,
                amount_label=filename_parts.amount_label,
                currency=amount.currency,
                charged_amount=amount.charged,
                refund_amount=amount.refunded,
                web_view_link=str(file.get("webViewLink") or ""),
                modified_time=str(file.get("modifiedTime") or ""),
                size=size,
            )
        )

    return ReceiptArchive(
        receipts=tuple(sorted(receipts, key=_receipt_sort_key)),
        review_files=tuple(sorted(review_files, key=_review_sort_key)),
        ignored_non_pdf_count=ignored_non_pdf_count,
    )


def filter_receipts(
    receipts: Iterable[OneOffReceipt],
    query: str = "",
    month: str = "",
    currency: str | ReceiptCurrency = "",
    refund: str = "",
) -> tuple[OneOffReceipt, ...]:
    normalized_query = _normalize_search_text(query)
    normalized_month = unicodedata.normalize("NFKC", str(month or "")).strip()
    normalized_currency = (
        currency.value if isinstance(currency, ReceiptCurrency) else str(currency or "")
    ).strip().upper()
    normalized_refund = _normalize_search_text(refund)

    show_all_currency = normalized_currency in {"", "ALL", "すべて"}
    refund_mode = _refund_mode(normalized_refund)
    if refund_mode == "invalid":
        return ()

    matches: list[OneOffReceipt] = []
    for receipt in receipts:
        if normalized_month and receipt.transaction_month != normalized_month:
            continue
        if not show_all_currency and receipt.currency.value != normalized_currency:
            continue
        if refund_mode == "refund" and not receipt.is_refund:
            continue
        if refund_mode == "normal" and receipt.is_refund:
            continue
        if normalized_query:
            searchable = _normalize_search_text(
                " ".join(
                    (
                        receipt.partner_name,
                        receipt.file_name,
                        receipt.amount_label,
                        receipt.currency.value,
                    )
                )
            )
            if normalized_query not in searchable:
                continue
        matches.append(receipt)
    return tuple(sorted(matches, key=_receipt_sort_key))


def archive_months(receipts: Iterable[OneOffReceipt]) -> tuple[str, ...]:
    return tuple(sorted({receipt.transaction_month for receipt in receipts}, reverse=True))


def duplicate_file_names(receipts: Iterable[OneOffReceipt]) -> tuple[str, ...]:
    counts = Counter(receipt.file_name for receipt in receipts)
    return tuple(
        sorted(
            (file_name for file_name, count in counts.items() if count > 1),
            key=lambda value: (_normalize_search_text(value), value),
        )
    )


def _parse_filename(file_name: str) -> _FilenameParts | None:
    if not _is_pdf_name(file_name):
        return None

    raw_stem = file_name[:-4]
    normalized_stem = unicodedata.normalize("NFKC", raw_stem)
    try:
        date_text, normalized_tail = normalized_stem.split("_", 1)
        normalized_partner, normalized_amount = normalized_tail.rsplit("_", 1)
    except ValueError:
        return None

    if not normalized_partner.strip() or not normalized_amount.strip():
        return None
    try:
        transaction_date = datetime.strptime(date_text, "%Y%m%d").date()
    except ValueError:
        return None

    try:
        _, raw_tail = raw_stem.split("_", 1)
        raw_partner, raw_amount = raw_tail.rsplit("_", 1)
    except ValueError:
        raw_partner, raw_amount = normalized_partner, normalized_amount

    if _parse_amount(normalized_amount) is None:
        return None
    return _FilenameParts(
        transaction_date=transaction_date,
        partner_name=raw_partner.strip(),
        normalized_partner=_normalize_partner(normalized_partner),
        amount_label=raw_amount.strip(),
        normalized_amount=normalized_amount.strip(),
    )


def _parse_amount(value: str) -> _Amount | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = _MINUS_SIGNS.sub("-", normalized)
    for currency, pattern in _CURRENCY_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        allow_fraction = currency is not ReceiptCurrency.JPY
        charged = _parse_decimal(match.group("charged"), allow_fraction=allow_fraction)
        refund_text = match.group("refund")
        refunded = (
            _parse_decimal(refund_text, allow_fraction=allow_fraction)
            if refund_text is not None
            else Decimal(0)
        )
        if charged is None or refunded is None:
            return None
        return _Amount(currency=currency, charged=charged, refunded=refunded)
    return None


def _parse_decimal(value: str, *, allow_fraction: bool) -> Decimal | None:
    text = str(value or "")
    if "." in text:
        if not allow_fraction:
            return None
        whole, fraction = text.split(".", 1)
        if not re.fullmatch(r"[0-9]{1,2}", fraction):
            return None
    else:
        whole = text
    if "," in whole and not re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+", whole):
        return None
    if "," not in whole and not whole.isdigit():
        return None
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _matching_recurring_service_id(
    file: Mapping[str, object],
    file_name: str,
) -> str:
    transaction_month = _transaction_month(file_name)
    if not transaction_month:
        return ""
    for service in SERVICES:
        usage_month = usage_month_for_transaction(service.id, transaction_month)
        if find_receipt([file], service, usage_month) is not None:
            return service.id
    return ""


def _transaction_month(file_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", file_name)
    match = re.match(r"^(?P<date>[0-9]{8})_", normalized)
    if match is None:
        return ""
    try:
        parsed = datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m")


def _known_service_id(normalized_partner: str) -> str:
    if not normalized_partner:
        return ""
    for service in SERVICES:
        aliases = (service.default_partner, *service.partner_aliases)
        if normalized_partner in {_normalize_partner(alias) for alias in aliases}:
            return service.id
    return ""


def _partner_hint(file_name: str) -> str:
    if not _is_pdf_name(file_name):
        return ""
    stem = unicodedata.normalize("NFKC", file_name[:-4])
    try:
        _, tail = stem.split("_", 1)
        partner, _ = tail.rsplit("_", 1)
    except ValueError:
        return ""
    return _normalize_partner(partner)


def _normalize_partner(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s_]+", "", normalized).casefold()


def _is_pdf_name(file_name: str) -> bool:
    return unicodedata.normalize("NFKC", str(file_name or "")).casefold().endswith(".pdf")


def _normalize_search_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _refund_mode(value: str) -> str:
    if value in {"", "all", "すべて"}:
        return "all"
    if value in {
        "refund",
        "refunded",
        "with_refund",
        "yes",
        "true",
        "1",
        "あり",
        "返金あり",
        "返金のみ",
    }:
        return "refund"
    if value in {
        "standard",
        "normal",
        "no_refund",
        "without_refund",
        "no",
        "false",
        "0",
        "なし",
        "返金なし",
    }:
        return "normal"
    return "invalid"


def _review_file(
    file: Mapping[str, object],
    reason: str,
    *,
    size: int | None,
) -> ReviewFile:
    return ReviewFile(
        file_id=str(file.get("id") or ""),
        file_name=str(file.get("name") or ""),
        reason=reason,
        web_view_link=str(file.get("webViewLink") or ""),
        modified_time=str(file.get("modifiedTime") or ""),
        size=size,
    )


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _receipt_sort_key(receipt: OneOffReceipt) -> tuple[object, ...]:
    return (
        -receipt.transaction_date.toordinal(),
        _normalize_search_text(receipt.partner_name),
        _normalize_search_text(receipt.file_name),
        receipt.file_name,
        receipt.file_id,
    )


def _review_sort_key(file: ReviewFile) -> tuple[str, str, str]:
    return (_normalize_search_text(file.file_name), file.file_name, file.file_id)
