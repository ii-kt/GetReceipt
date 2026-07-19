from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..config import expected_transaction_month, parse_month_key, service_by_id
from ..domain.document_metadata import extract_amount_yen, extract_pdf_text
from .auto_acquisition import run_auto_acquisition


MAX_MANUAL_PDF_BYTES = 20 * 1024 * 1024


class ManualUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManualUploadInspection:
    amount_yen: int | None
    partner_found: bool
    month_found: bool
    warnings: tuple[str, ...]

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.warnings)


@dataclass(frozen=True)
class _UploadedStatement:
    content: bytes
    source_url: str
    original_file_name: str
    metadata_text: str
    logs: tuple[str, ...] = ()


class _UploadedFetcher:
    def __init__(self, statement: _UploadedStatement) -> None:
        self.statement = statement

    def fetch_pdf(self, _target_month: str) -> _UploadedStatement:
        return self.statement


def inspect_manual_receipt(
    *,
    service_id: str,
    target_month: str,
    content: bytes,
) -> ManualUploadInspection:
    service = service_by_id(service_id)
    parse_month_key(target_month)
    _validate_pdf(content)
    text = _normalize(extract_pdf_text(content))
    amount = extract_amount_yen(text)

    partner_hints = {
        _normalize(alias)
        for alias in (service.default_partner, *service.partner_aliases)
    }
    partner_hints.update(
        _normalize(alias)
        .replace("株式会社", "")
        .replace("中部テレコミュニケーション", "コミュファ")
        for alias in tuple(partner_hints)
    )
    partner_hints.discard("")
    partner_found = any(hint in text for hint in partner_hints)

    candidate_months = {
        target_month,
        expected_transaction_month(service_id, target_month),
    }
    month_found = any(
        _month_in_text(text, month)
        for month in candidate_months
    )

    warnings: list[str] = []
    if amount is None:
        raise ManualUploadError("PDFから請求金額を確認できません。別のPDFを選んでください。")
    if not partner_found:
        warnings.append("PDF本文から請求元名を自動確認できませんでした。")
    if not month_found:
        warnings.append("PDF本文から対象月を自動確認できませんでした。")
    return ManualUploadInspection(
        amount_yen=amount,
        partner_found=partner_found,
        month_found=month_found,
        warnings=tuple(warnings),
    )


def save_manual_receipt(
    *,
    service_id: str,
    target_month: str,
    content: bytes,
    original_file_name: str,
    storage: Any,
    confirmed: bool,
) -> Any:
    inspection = inspect_manual_receipt(
        service_id=service_id,
        target_month=target_month,
        content=content,
    )
    if inspection.requires_confirmation and not confirmed:
        raise ManualUploadError("請求元と対象月を目視確認してから保存してください。")
    statement = _UploadedStatement(
        content=content,
        source_url="",
        original_file_name=str(original_file_name or "iphone-upload.pdf"),
        metadata_text=extract_pdf_text(content),
    )
    return run_auto_acquisition(
        service_id=service_id,
        target_month=target_month,
        fetcher=_UploadedFetcher(statement),
        storage=storage,
    )


def _validate_pdf(content: bytes) -> None:
    if not isinstance(content, bytes) or not content.startswith(b"%PDF"):
        raise ManualUploadError("PDFファイルを選んでください。")
    if len(content) > MAX_MANUAL_PDF_BYTES:
        raise ManualUploadError("PDFは20MB以下にしてください。")


def _normalize(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).lower(),
    )


def _month_in_text(text: str, month_key: str) -> bool:
    year, month = parse_month_key(month_key)
    patterns = (
        f"{year}年{month}月",
        f"{year}年{month:02d}月",
        f"{year}/{month}",
        f"{year}/{month:02d}",
        f"{year}-{month:02d}",
        f"{year}{month:02d}",
    )
    return any(_normalize(pattern) in text for pattern in patterns)
