from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RECEIPT_DRIVE_FOLDER_ID, service_by_id
from .drive_storage import DriveStorage
from .ledger import ReceiptLedger
from .naming import ReceiptMetadata, build_receipt_filename, normalize_amount_yen, normalize_extension, sha256_bytes


class ReceiptPipelineError(RuntimeError):
    pass


def metadata_from_backend_record(record: dict[str, Any]) -> ReceiptMetadata:
    service_id = record.get("service") or record.get("service_id") or "epos"
    service = service_by_id(service_id)
    naming = record.get("naming") or {}
    raw_date = str(naming.get("date") or "")
    if len(raw_date) != 8:
        raise ReceiptPipelineError("Backend result did not include a valid transaction date.")

    transaction_date = datetime.strptime(raw_date, "%Y%m%d").date()
    amount_yen = normalize_amount_yen(naming.get("amount"))
    partner = str(naming.get("partner") or service.default_partner)

    return ReceiptMetadata(
        service_id=service.id,
        service_label=service.label,
        target_month=str(record.get("yearMonth") or ""),
        transaction_date=transaction_date,
        partner_name=partner,
        amount_yen=amount_yen,
        source_url=str(record.get("sourceUrl") or ""),
        original_file_name=str(record.get("originalFileName") or record.get("fileName") or ""),
    )


def upload_backend_record_to_drive(
    *,
    record: dict[str, Any],
    storage: DriveStorage,
    ledger: ReceiptLedger,
) -> dict[str, str]:
    file_path = Path(str(record.get("filePath") or ""))
    if not file_path.exists() or not file_path.is_file():
        raise ReceiptPipelineError(f"Downloaded file was not found: {file_path}")

    metadata = metadata_from_backend_record(record)
    extension = normalize_extension(record.get("fileName") or file_path.name)
    drive_name = build_receipt_filename(metadata, extension)
    content = file_path.read_bytes()
    result = storage.upload_bytes(
        file_name=drive_name,
        content=content,
        mime_type=_mime_type(extension),
    )
    saved = ledger.append_upload(
        metadata=metadata,
        file_name=drive_name,
        drive_file_id=result.id,
        drive_web_view_link=result.web_view_link,
        sha256=sha256_bytes(content),
    )
    storage.upsert_bytes(
        file_name="_receipt_index.csv",
        content=ledger.to_csv_bytes(),
        mime_type="text/csv",
    )
    return saved


def record_not_issued_to_drive(
    *,
    service_id: str,
    target_month: str,
    storage: DriveStorage | None,
    ledger: ReceiptLedger,
) -> dict[str, str]:
    service = service_by_id(service_id)
    record = ledger.mark_not_issued(
        service_id=service.id,
        service_label=service.label,
        target_month=target_month,
    )
    if storage is not None:
        storage.upsert_bytes(
            file_name="_receipt_index.csv",
            content=ledger.to_csv_bytes(),
            mime_type="text/csv",
        )
    return record


def drive_storage_from_secrets(secrets: Any) -> DriveStorage:
    return DriveStorage.from_secrets(secrets, folder_id=RECEIPT_DRIVE_FOLDER_ID)


def _mime_type(extension: str) -> str:
    return {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(extension.lower(), "application/octet-stream")


