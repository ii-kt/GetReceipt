from __future__ import annotations

from typing import Any

from .acquisition import default_transaction_date
from .config import RECEIPT_DRIVE_FOLDER_ID, service_by_id
from .document_metadata import extract_receipt_data
from .drive_storage import DriveStorage
from .ledger import ReceiptLedger
from .naming import ReceiptMetadata, build_receipt_filename, sha256_bytes


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


def upload_auto_receipt_to_drive(
    *,
    service_id: str,
    target_month: str,
    content: bytes,
    original_file_name: str,
    source_url: str,
    metadata_text: str,
    storage: DriveStorage,
    ledger: ReceiptLedger,
) -> dict[str, str]:
    service = service_by_id(service_id)
    extracted = extract_receipt_data(content, metadata_text)
    if extracted.amount_yen is None:
        raise ValueError(
            "取得した明細から金額を読み取れませんでした。PDFの内容を確認するか、手動登録で金額を入力してください。"
        )

    metadata = ReceiptMetadata(
        service_id=service.id,
        service_label=service.label,
        target_month=target_month,
        transaction_date=extracted.transaction_date or default_transaction_date(service.id, target_month),
        partner_name=service.default_partner,
        amount_yen=extracted.amount_yen,
        source_url=source_url,
        original_file_name=original_file_name,
    )
    file_name = build_receipt_filename(metadata, "pdf")
    result = storage.upload_bytes(
        file_name=file_name,
        content=content,
        mime_type="application/pdf",
    )
    saved = ledger.append_upload(
        metadata=metadata,
        file_name=file_name,
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


def drive_storage_from_secrets(secrets: Any) -> DriveStorage:
    return DriveStorage.from_secrets(secrets, folder_id=RECEIPT_DRIVE_FOLDER_ID)
