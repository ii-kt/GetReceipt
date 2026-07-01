from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Iterable

from .naming import ReceiptMetadata


CSV_FIELDS = [
    "uploaded_at",
    "status",
    "service_id",
    "service_label",
    "target_month",
    "transaction_date",
    "partner_name",
    "amount_yen",
    "currency",
    "file_name",
    "drive_file_id",
    "drive_web_view_link",
    "sha256",
    "source_url",
    "original_file_name",
]


class ReceiptLedger:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

    def append_upload(
        self,
        *,
        metadata: ReceiptMetadata,
        file_name: str,
        drive_file_id: str,
        drive_web_view_link: str,
        sha256: str,
    ) -> dict[str, str]:
        record = {
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "status": "uploaded",
            **metadata.to_record(),
            "file_name": file_name,
            "drive_file_id": drive_file_id,
            "drive_web_view_link": drive_web_view_link,
            "sha256": sha256,
        }
        self._write([record, *self.read()])
        return record

    def mark_not_issued(
        self,
        *,
        service_id: str,
        service_label: str,
        target_month: str,
    ) -> dict[str, str]:
        record = {field: "" for field in CSV_FIELDS}
        record.update({
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "status": "not_issued",
            "service_id": service_id,
            "service_label": service_label,
            "target_month": target_month,
        })
        self._write([record, *self.read()])
        return record

    def rename_file(self, *, drive_file_id: str, file_name: str) -> bool:
        rows = self.read()
        changed = False
        for row in rows:
            if row.get("drive_file_id") == drive_file_id:
                row["file_name"] = file_name
                changed = True
        if changed:
            self._write(rows)
        return changed

    def replace_all(self, rows: Iterable[dict[str, str]]) -> None:
        self._write(rows)

    def to_csv_bytes(self) -> bytes:
        rows = self.read()
        return rows_to_csv_bytes(rows)

    def _write(self, rows: Iterable[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


def rows_to_csv_bytes(rows: Iterable[dict[str, str]]) -> bytes:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def rows_from_csv_bytes(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    return list(csv.DictReader(StringIO(text)))


