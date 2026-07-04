from __future__ import annotations

import csv
import re
from datetime import date, datetime, timezone
from io import StringIO
from typing import Protocol

from ..domain.ledger import CSV_FIELDS, ReceiptLedger, rows_from_csv_bytes, rows_to_csv_bytes
from ..domain.naming import (
    ReceiptMetadata,
    build_receipt_filename,
    normalize_amount_yen,
    normalize_extension,
)


LEDGER_SYNC_FILE_NAME = "_receipt_index.csv"
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

HIDDEN_AUDIT_COLUMNS = {
    "ファイルID",
    "サービスID",
    "サービス名",
    "対象月",
    "通貨",
    "取得元URL",
    "元ファイル名",
}


class FilenameAuditStorage(Protocol):
    def list_files(self) -> list[dict[str, str]]:
        ...

    def download_bytes_by_name(self, file_name: str) -> bytes | None:
        ...

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str):
        ...


def refresh_drive_filename_audit(
    storage: FilenameAuditStorage,
    ledger: ReceiptLedger,
) -> list[dict[str, str]]:
    files = storage.list_files()
    ledger_rows = repair_missing_ledger_drive_ids(storage, ledger, files, load_synced_ledger_rows(storage, ledger))
    return build_drive_filename_audit_rows(files, ledger_rows)


def repair_missing_ledger_drive_ids(
    storage: FilenameAuditStorage,
    ledger: ReceiptLedger,
    files: list[dict[str, str]],
    ledger_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    rows_by_file_name = {
        row.get("file_name", ""): row
        for row in ledger_rows
        if row.get("file_name") and not row.get("drive_file_id")
    }
    changed = False
    for file in files:
        name = file.get("name", "")
        if not name or name == LEDGER_SYNC_FILE_NAME or file.get("mimeType") == DRIVE_FOLDER_MIME_TYPE:
            continue
        row = rows_by_file_name.get(name)
        if not row:
            continue
        row["drive_file_id"] = file.get("id", "")
        row["drive_web_view_link"] = file.get("webViewLink", row.get("drive_web_view_link", ""))
        changed = True

    if changed:
        ledger.replace_all(ledger_rows)
        storage.upsert_bytes(
            file_name=LEDGER_SYNC_FILE_NAME,
            content=rows_to_csv_bytes(ledger_rows),
            mime_type="text/csv",
        )
    return ledger_rows


def load_synced_ledger_rows(
    storage: FilenameAuditStorage,
    ledger: ReceiptLedger,
) -> list[dict[str, str]]:
    rows_by_drive_id: dict[str, dict[str, str]] = {}
    rows_without_drive_id: list[dict[str, str]] = []

    def collect(rows: list[dict[str, str]]) -> None:
        for row in rows:
            drive_file_id = row.get("drive_file_id", "")
            if drive_file_id:
                rows_by_drive_id[drive_file_id] = row
            else:
                rows_without_drive_id.append(row)

    collect(ledger.read())
    drive_content = storage.download_bytes_by_name(LEDGER_SYNC_FILE_NAME)
    if drive_content:
        collect(rows_from_csv_bytes(drive_content))
    return [*rows_by_drive_id.values(), *rows_without_drive_id]


def build_drive_filename_audit_rows(
    files: list[dict[str, str]],
    ledger_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    ledger_by_drive_id = {
        row.get("drive_file_id", ""): row
        for row in ledger_rows
        if row.get("drive_file_id")
    }
    rows: list[dict[str, str]] = []
    for file in files:
        name = file.get("name", "")
        mime_type = file.get("mimeType", "")
        if mime_type == DRIVE_FOLDER_MIME_TYPE:
            status = "管理"
            reason = "フォルダです。"
            transaction_date = partner_name = amount_yen = extension = ""
            expected_name = ""
            ledger_status = "管理"
            record = {}
        elif name == LEDGER_SYNC_FILE_NAME:
            status = "管理"
            reason = "保存台帳の同期ファイルです。"
            transaction_date = partner_name = amount_yen = extension = ""
            expected_name = ""
            ledger_status = "管理"
            record = {}
        else:
            extension = normalize_extension(name, "pdf")
            record = ledger_by_drive_id.get(file.get("id", ""))
            if record:
                ledger_status = "登録済"
                try:
                    expected_name = expected_filename_from_ledger_record(record, extension)
                    status = "OK" if name == expected_name else "要確認"
                    reason = (
                        "保存台帳のメタデータから生成したファイル名と一致しています。"
                        if status == "OK"
                        else "保存台帳のメタデータから生成したファイル名と一致していません。"
                    )
                    transaction_date = transaction_date_key_from_record(record)
                    partner_name = record.get("partner_name", "")
                    amount_yen = record.get("amount_yen", "")
                except Exception as error:
                    status = "要確認"
                    reason = f"保存台帳のメタデータから期待ファイル名を生成できません: {error}"
                    transaction_date = record.get("transaction_date", "")
                    partner_name = record.get("partner_name", "")
                    amount_yen = record.get("amount_yen", "")
                    expected_name = ""
            else:
                status = "要確認"
                reason = "保存台帳に未登録です。取引日・取引先・金額を入れると、台帳登録と名前変更を同時に実行できます。"
                transaction_date = partner_name = amount_yen = ""
                expected_name = ""
                ledger_status = "未登録"
                record = {}
        rows.append({
            "ファイルID": file.get("id", ""),
            "判定": status,
            "台帳": ledger_status,
            "ファイル名": name,
            "期待ファイル名": expected_name,
            "理由": reason,
            "取引日": transaction_date,
            "取引先": partner_name,
            "金額": amount_yen,
            "拡張子": extension,
            "更新日時": file.get("modifiedTime", ""),
            "Drive": file.get("webViewLink", ""),
            "サービスID": record.get("service_id", ""),
            "サービス名": record.get("service_label", ""),
            "対象月": record.get("target_month", ""),
            "通貨": record.get("currency", "JPY"),
            "取得元URL": record.get("source_url", ""),
            "元ファイル名": record.get("original_file_name", ""),
        })

    order = {"要確認": 0, "OK": 1, "管理": 2}
    return sorted(rows, key=lambda row: (order.get(row["判定"], 9), row["ファイル名"]))


def audit_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "ok": sum(row.get("判定") == "OK" for row in rows),
        "review": sum(row.get("判定") == "要確認" for row in rows),
        "managed": sum(row.get("判定") == "管理" for row in rows),
    }


def expected_filename_from_ledger_record(record: dict[str, str], extension: str) -> str:
    metadata = ReceiptMetadata(
        service_id=record.get("service_id", ""),
        service_label=record.get("service_label", ""),
        target_month=record.get("target_month", ""),
        transaction_date=parse_ledger_transaction_date(record.get("transaction_date", "")),
        partner_name=record.get("partner_name", ""),
        amount_yen=normalize_amount_yen(record.get("amount_yen", "")),
        currency=record.get("currency", "JPY") or "JPY",
        source_url=record.get("source_url", ""),
        original_file_name=record.get("original_file_name", ""),
    )
    return build_receipt_filename(metadata, extension)


def transaction_date_key_from_record(record: dict[str, str]) -> str:
    return parse_ledger_transaction_date(record.get("transaction_date", "")).strftime("%Y%m%d")


def parse_ledger_transaction_date(value: str) -> date:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def audit_display_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{key: value for key, value in row.items() if key not in HIDDEN_AUDIT_COLUMNS} for row in rows]


def audit_rows_to_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = StringIO()
    fieldnames = list(rows[0].keys()) if rows else []
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def rename_synced_ledger_file(
    storage: FilenameAuditStorage,
    ledger: ReceiptLedger,
    *,
    drive_file_id: str,
    file_name: str,
    metadata: ReceiptMetadata,
    drive_web_view_link: str,
    original_file_name: str,
) -> bool:
    rows = load_synced_ledger_rows(storage, ledger)
    metadata_record = metadata.to_record()
    for row in rows:
        if row.get("drive_file_id") == drive_file_id:
            row.update(metadata_record)
            row["status"] = "uploaded"
            row["file_name"] = file_name
            row["drive_web_view_link"] = drive_web_view_link or row.get("drive_web_view_link", "")
            row["source_url"] = metadata.source_url or row.get("source_url", "")
            row["original_file_name"] = metadata.original_file_name or original_file_name
            break
    else:
        row = {field: "" for field in CSV_FIELDS}
        row.update({
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "status": "uploaded",
            **metadata_record,
            "file_name": file_name,
            "drive_file_id": drive_file_id,
            "drive_web_view_link": drive_web_view_link,
            "source_url": metadata.source_url,
            "original_file_name": metadata.original_file_name or original_file_name,
        })
        rows.insert(0, row)

    ledger.replace_all(rows)
    storage.upsert_bytes(
        file_name=LEDGER_SYNC_FILE_NAME,
        content=rows_to_csv_bytes(rows),
        mime_type="text/csv",
    )
    return True


def default_rename_date(row: dict[str, str]) -> date:
    value = row.get("取引日", "")
    parsed_date = parse_rename_date(value) or parse_rename_file_name(row).get("transaction_date")
    if parsed_date:
        return parsed_date
    return date.today()


def default_rename_partner(row: dict[str, str]) -> str:
    if row.get("取引先"):
        return row["取引先"]
    return parse_rename_file_name(row).get("partner_name", "")


def default_rename_amount(row: dict[str, str]) -> int:
    value = row.get("金額", "")
    if value.isdigit():
        return int(value)
    return parse_rename_file_name(row).get("amount_yen", 0)


def default_rename_extension(row: dict[str, str]) -> str:
    return row.get("拡張子") or normalize_extension(row.get("ファイル名"), "pdf")


def parse_rename_date(value: str) -> date | None:
    if not re.fullmatch(r"\d{8}", value or ""):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def parse_rename_file_name(row: dict[str, str]) -> dict[str, object]:
    match = re.fullmatch(
        r"(?P<date>\d{8})_(?P<partner>.+)_(?P<amount>\d+)円\.[a-z0-9]+",
        row.get("ファイル名", ""),
    )
    if not match:
        return {}
    transaction_date = parse_rename_date(match.group("date"))
    if not transaction_date:
        return {}
    return {
        "transaction_date": transaction_date,
        "partner_name": match.group("partner"),
        "amount_yen": int(match.group("amount")),
    }


def build_rename_metadata(
    *,
    row: dict[str, str],
    transaction_date: date,
    partner_name: str,
    amount_yen: int,
) -> ReceiptMetadata:
    return ReceiptMetadata(
        service_id=row.get("サービスID", ""),
        service_label=row.get("サービス名", ""),
        target_month=row.get("対象月") or transaction_date.strftime("%Y-%m"),
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=normalize_amount_yen(amount_yen),
        currency=row.get("通貨", "JPY") or "JPY",
        source_url=row.get("取得元URL") or row.get("Drive", ""),
        original_file_name=row.get("元ファイル名") or row.get("ファイル名", ""),
    )


def build_rename_preview_name(
    *,
    transaction_date: date,
    partner_name: str,
    amount_yen: int,
    extension: str,
) -> str:
    metadata = ReceiptMetadata(
        service_id="",
        service_label="",
        target_month="",
        transaction_date=transaction_date,
        partner_name=partner_name,
        amount_yen=amount_yen,
    )
    return build_receipt_filename(metadata, extension)
