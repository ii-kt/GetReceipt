from __future__ import annotations

from typing import Any

from .config import RECEIPT_DRIVE_FOLDER_ID, service_by_id
from .drive_storage import DriveStorage
from .ledger import ReceiptLedger


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
