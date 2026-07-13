from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


FORBIDDEN_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')


def safe_name_part(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = FORBIDDEN_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._ ")


def normalize_extension(file_name: str | None, fallback: str = "pdf") -> str:
    suffix = Path(file_name or "").suffix.lower().lstrip(".")
    extension = suffix or fallback.lower().lstrip(".")
    extension = re.sub(r"[^a-z0-9]+", "", extension)
    return extension or fallback


@dataclass(frozen=True)
class ReceiptMetadata:
    transaction_date: date
    partner_name: str
    amount_yen: int

    @property
    def transaction_date_key(self) -> str:
        return self.transaction_date.strftime("%Y%m%d")

    @property
    def amount_label(self) -> str:
        return f"{self.amount_yen}円"


def build_receipt_filename(metadata: ReceiptMetadata, extension: str = "pdf") -> str:
    partner = safe_name_part(metadata.partner_name) or "取引先未設定"
    clean_extension = normalize_extension(f"file.{extension}", extension)
    base_name = f"{metadata.transaction_date_key}_{partner}_{metadata.amount_label}"
    return f"{safe_name_part(base_name)}.{clean_extension}"
