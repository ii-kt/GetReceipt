from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


FORBIDDEN_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')


def safe_name_part(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = FORBIDDEN_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._ ")


def normalize_amount_yen(value: Any) -> int:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace(",", "").replace("\u5186", "").replace("\uffe5", "").replace("\u00a5", "").strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError("\u91d1\u984d\u306f0\u4ee5\u4e0a\u306e\u6574\u6570\u3067\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002")
    return int(text)


def normalize_extension(file_name: str | None, fallback: str = "pdf") -> str:
    suffix = Path(file_name or "").suffix.lower().lstrip(".")
    extension = suffix or fallback.lower().lstrip(".")
    extension = re.sub(r"[^a-z0-9]+", "", extension)
    return extension or fallback


def parse_transaction_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError("\u53d6\u5f15\u65e5\u306f YYYY-MM-DD \u5f62\u5f0f\u3067\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002")


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


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

