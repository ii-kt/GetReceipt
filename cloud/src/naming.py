from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


FORBIDDEN_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')
RECEIPT_FILENAME_PATTERN = re.compile(
    r"^(?P<transaction_date>\d{8})_(?P<partner>.+)_(?P<amount_yen>\d+)円\.(?P<extension>[a-z0-9]+)$"
)
MANAGED_FILE_NAMES = {"_receipt_index.csv"}


def safe_name_part(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = FORBIDDEN_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._ ")


def normalize_amount_yen(value: Any) -> int:
    text = unicodedata.normalize("NFKC", str("" if value is None else value))
    text = text.replace(",", "").replace("\u5186", "").replace("\uffe5", "").replace("\u00a5", "").strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError("\u91d1\u984d\u306f0\u4ee5\u4e0a\u306e\u6574\u6570\u3067\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002")
    return int(text)


def normalize_extension(file_name: str | None, fallback: str = "pdf") -> str:
    suffix = Path(file_name or "").suffix.lower().lstrip(".")
    extension = suffix or fallback.lower().lstrip(".")
    extension = re.sub(r"[^a-z0-9]+", "", extension)
    return extension or fallback


@dataclass(frozen=True)
class ReceiptMetadata:
    service_id: str
    service_label: str
    target_month: str
    transaction_date: date
    partner_name: str
    amount_yen: int
    currency: str = "JPY"
    source_url: str = ""
    original_file_name: str = ""

    @property
    def transaction_date_key(self) -> str:
        return self.transaction_date.strftime("%Y%m%d")

    @property
    def amount_label(self) -> str:
        return f"{self.amount_yen}\u5186"

    def to_record(self) -> dict[str, str]:
        record = asdict(self)
        record["transaction_date"] = self.transaction_date.isoformat()
        record["amount_yen"] = str(self.amount_yen)
        return record


def build_receipt_filename(metadata: ReceiptMetadata, extension: str) -> str:
    partner = safe_name_part(metadata.partner_name) or "\u53d6\u5f15\u5148\u672a\u8a2d\u5b9a"
    clean_extension = normalize_extension(f"file.{extension}", extension)
    base_name = f"{metadata.transaction_date_key}_{partner}_{metadata.amount_label}"
    return f"{safe_name_part(base_name)}.{clean_extension}"


@dataclass(frozen=True)
class FilenameInspection:
    status: str
    reason: str
    transaction_date: str = ""
    partner_name: str = ""
    amount_yen: str = ""
    extension: str = ""


def inspect_receipt_filename(file_name: str) -> FilenameInspection:
    if file_name in MANAGED_FILE_NAMES:
        return FilenameInspection(status="管理", reason="保存台帳の同期ファイルです。")

    match = RECEIPT_FILENAME_PATTERN.fullmatch(file_name)
    if not match:
        return FilenameInspection(
            status="要確認",
            reason="YYYYMMDD_取引先_金額円.拡張子 の形式ではありません。",
            extension=normalize_extension(file_name, ""),
        )

    transaction_date = match.group("transaction_date")
    partner_name = match.group("partner")
    amount_yen = match.group("amount_yen")
    extension = match.group("extension")

    try:
        datetime.strptime(transaction_date, "%Y%m%d")
    except ValueError:
        return FilenameInspection(
            status="要確認",
            reason="取引日が実在する日付ではありません。",
            transaction_date=transaction_date,
            partner_name=partner_name,
            amount_yen=amount_yen,
            extension=extension,
        )

    if not partner_name or safe_name_part(partner_name) != partner_name:
        return FilenameInspection(
            status="要確認",
            reason="取引先名が保存用に正規化されていません。",
            transaction_date=transaction_date,
            partner_name=partner_name,
            amount_yen=amount_yen,
            extension=extension,
        )

    return FilenameInspection(
        status="OK",
        reason="命名規則に一致しています。",
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=amount_yen,
        extension=extension,
    )


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

