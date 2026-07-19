from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any, Protocol

from ..config import expected_transaction_month, parse_month_key, service_by_id
from ..domain.acquisition import (
    AcquisitionFailure,
    AcquisitionOutcome,
    AcquisitionResult,
    ProgressEvent,
    SecurityChallenge,
    Stage,
    StoredReceiptReference,
)
from ..domain.document_metadata import ExtractedReceiptData, extract_receipt_data
from ..domain.naming import ReceiptMetadata, build_receipt_filename
from ..domain.periods import date_in_target_month


class ReceiptFetcher(Protocol):
    def fetch_pdf(self, target_month: str) -> Any:
        ...


class ReceiptStorage(Protocol):
    def list_files(self) -> list[dict[str, str]]:
        ...

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> Any:
        ...


ProgressCallback = Callable[[ProgressEvent], None]
ReceiptFinder = Callable[[list[dict[str, str]], Any, str], Any | None]
StatementFetcher = Callable[[str], Any]
CancellationCheck = Callable[[], bool]
_ACQUISITION_LOCK = Lock()
_ACTION_REQUIRED_CHALLENGE_KINDS = {
    "verification_code",
    "captcha",
    "interactive",
    "consent",
    "push_approval",
    "passkey_unavailable",
}


def run_auto_acquisition(
    *,
    service_id: str,
    target_month: str,
    fetcher: ReceiptFetcher,
    storage: ReceiptStorage,
    on_progress: ProgressCallback | None = None,
    receipt_finder: ReceiptFinder | None = None,
    fetch_statement: StatementFetcher | None = None,
    cancellation_requested: CancellationCheck | None = None,
) -> AcquisitionResult:
    """Serialize browser acquisition and keep Drive as the only saved-state truth."""

    with _ACQUISITION_LOCK:
        return _run_auto_acquisition_unlocked(
            service_id=service_id,
            target_month=target_month,
            fetcher=fetcher,
            storage=storage,
            on_progress=on_progress,
            receipt_finder=receipt_finder,
            fetch_statement=fetch_statement,
            cancellation_requested=cancellation_requested,
        )


def _run_auto_acquisition_unlocked(
    *,
    service_id: str,
    target_month: str,
    fetcher: ReceiptFetcher,
    storage: ReceiptStorage,
    on_progress: ProgressCallback | None = None,
    receipt_finder: ReceiptFinder | None = None,
    fetch_statement: StatementFetcher | None = None,
    cancellation_requested: CancellationCheck | None = None,
) -> AcquisitionResult:
    """Acquire and persist one service usage month using Drive as the only truth.

    ``target_month`` remains the usage month across the workflow. Drive matching
    and filename dates use the service-specific transaction month. An existing
    matching PDF short-circuits the provider call, and a save is successful only
    after a fresh Drive listing can find the expected receipt.
    """

    events: list[ProgressEvent] = []

    def emit(stage: Stage, message: str) -> None:
        event = ProgressEvent(stage=stage, message=message)
        events.append(event)
        if on_progress is not None:
            try:
                on_progress(event)
            except Exception:
                # UI notification failures must not change acquisition semantics.
                pass

    def failed(*, code: str, message: str, stage: Stage) -> AcquisitionResult:
        emit(Stage.FAILED, message)
        return AcquisitionResult(
            service_id=service_id,
            target_month=target_month,
            outcome=AcquisitionOutcome.FAILED,
            events=tuple(events),
            failure=AcquisitionFailure(code=code, message=message, stage=stage),
        )

    def cancelled(stage: Stage) -> AcquisitionResult | None:
        if cancellation_requested is None:
            return None
        try:
            requested = bool(cancellation_requested())
        except Exception:
            requested = True
        if not requested:
            return None
        return failed(
            code="ACQUISITION_CANCELLED",
            message="キャンセルされたため、Google Driveへ保存せず処理を終了しました。",
            stage=stage,
        )

    try:
        service = service_by_id(service_id)
        parse_month_key(target_month)
    except (KeyError, TypeError, ValueError):
        return failed(
            code="INVALID_REQUEST",
            message="取得先または対象月の指定が不正です。",
            stage=Stage.CHECKING_DRIVE,
        )

    if receipt_finder is None:
        from .drive_status import find_receipt

        receipt_finder = find_receipt

    if cancellation := cancelled(Stage.CHECKING_DRIVE):
        return cancellation

    emit(Stage.CHECKING_DRIVE, "Google Driveで取得済みPDFを確認しています。")
    try:
        existing = receipt_finder(storage.list_files(), service, target_month)
    except Exception:
        return failed(
            code="DRIVE_CHECK_FAILED",
            message="Google Driveの取得済み確認に失敗しました。",
            stage=Stage.CHECKING_DRIVE,
        )

    if existing is not None:
        receipt = _stored_receipt_reference(existing)
        emit(Stage.COMPLETED, "対象月のPDFはGoogle Driveに保存済みです。")
        return AcquisitionResult(
            service_id=service.id,
            target_month=target_month,
            outcome=AcquisitionOutcome.ALREADY_EXISTS,
            events=tuple(events),
            receipt=receipt,
        )

    if cancellation := cancelled(Stage.FETCHING):
        return cancellation

    emit(Stage.FETCHING, "請求元から対象月のPDFを自動取得しています。")
    try:
        statement_fetcher = fetch_statement if fetch_statement is not None else fetcher.fetch_pdf
        statement = statement_fetcher(target_month)
    except Exception as error:
        challenge_kind = str(getattr(error, "challenge_kind", "") or "")
        if challenge_kind in _ACTION_REQUIRED_CHALLENGE_KINDS:
            message = str(error) or "追加認証コードの入力が必要です。"
            stage = (
                Stage.AWAITING_SECURITY_CODE
                if challenge_kind == "verification_code"
                else Stage.AWAITING_USER_ACTION
            )
            emit(stage, message)
            return AcquisitionResult(
                service_id=service.id,
                target_month=target_month,
                outcome=AcquisitionOutcome.ACTION_REQUIRED,
                events=tuple(events),
                challenge=SecurityChallenge(kind=challenge_kind, message=message),
            )
        return failed(
            code=_error_code(error, "FETCH_FAILED"),
            message="PDFの自動取得に失敗しました。",
            stage=Stage.FETCHING,
        )

    content = getattr(statement, "content", b"")
    if not isinstance(content, bytes) or b"%PDF" not in content[:1024]:
        return failed(
            code="INVALID_PDF",
            message="取得結果をPDFとして確認できませんでした。",
            stage=Stage.FETCHING,
        )

    if cancellation := cancelled(Stage.FETCHING):
        return cancellation

    emit(Stage.EXTRACTING, "PDFから保存用メタデータを抽出しています。")
    try:
        extracted = extract_receipt_data(content, str(getattr(statement, "metadata_text", "")))
        metadata = _receipt_metadata(
            service=service,
            target_month=target_month,
            extracted=extracted,
        )
        file_name = build_receipt_filename(metadata, "pdf")
    except Exception:
        return failed(
            code="METADATA_EXTRACTION_FAILED",
            message="PDFの保存情報を確定できませんでした。",
            stage=Stage.EXTRACTING,
        )

    emit(Stage.CHECKING_DRIVE, "保存直前にGoogle Driveを再確認しています。")
    try:
        existing = receipt_finder(storage.list_files(), service, target_month)
    except Exception:
        return failed(
            code="DRIVE_RECHECK_FAILED",
            message="保存直前のGoogle Drive確認に失敗しました。",
            stage=Stage.CHECKING_DRIVE,
        )

    if existing is not None:
        receipt = _stored_receipt_reference(existing)
        emit(Stage.COMPLETED, "同じ対象月のPDFがGoogle Driveに保存済みです。")
        return AcquisitionResult(
            service_id=service.id,
            target_month=target_month,
            outcome=AcquisitionOutcome.ALREADY_EXISTS,
            events=tuple(events),
            receipt=receipt,
        )

    if cancellation := cancelled(Stage.SAVING):
        return cancellation

    emit(Stage.SAVING, "PDFをGoogle Driveへ保存しています。")
    try:
        storage.upsert_bytes(file_name=file_name, content=content, mime_type="application/pdf")
    except Exception as error:
        code = _error_code(error, "DRIVE_SAVE_FAILED")
        if code == "ACQUISITION_CANCELLED":
            return cancelled(Stage.SAVING) or failed(
                code=code,
                message="Google Driveへの保存開始前に処理がキャンセルされました。",
                stage=Stage.SAVING,
            )
        return failed(
            code=code,
            message="Google DriveへのPDF保存に失敗しました。",
            stage=Stage.SAVING,
        )

    emit(Stage.VERIFYING, "Google Drive上のPDF実在を再確認しています。")
    try:
        stored = receipt_finder(storage.list_files(), service, target_month)
    except Exception:
        return failed(
            code="DRIVE_VERIFY_FAILED",
            message="保存後のGoogle Drive再確認に失敗しました。",
            stage=Stage.VERIFYING,
        )

    if stored is None:
        return failed(
            code="SAVED_FILE_NOT_FOUND",
            message="保存操作後もGoogle Drive上で対象PDFを確認できませんでした。",
            stage=Stage.VERIFYING,
        )

    receipt = _stored_receipt_reference(stored)
    emit(Stage.COMPLETED, "PDFの自動取得とGoogle Drive保存が完了しました。")
    return AcquisitionResult(
        service_id=service.id,
        target_month=target_month,
        outcome=AcquisitionOutcome.ACQUIRED,
        events=tuple(events),
        receipt=receipt,
    )


def _receipt_metadata(
    *,
    service: Any,
    target_month: str,
    extracted: ExtractedReceiptData,
) -> ReceiptMetadata:
    if extracted.amount_yen is None:
        raise ValueError("取得したPDFから請求金額を読み取れませんでした。")

    return ReceiptMetadata(
        transaction_date=date_in_target_month(
            expected_transaction_month(service.id, target_month),
            extracted.transaction_date,
        ),
        partner_name=service.default_partner,
        amount_yen=extracted.amount_yen,
    )


def _stored_receipt_reference(receipt: Any) -> StoredReceiptReference:
    return StoredReceiptReference(
        file_id=str(_value(receipt, "file_id", "id")),
        file_name=str(_value(receipt, "file_name", "name")),
        web_view_link=str(_value(receipt, "web_view_link", "drive_web_view_link", default="")),
    )


def _value(item: Any, *names: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _error_code(error: Exception, fallback: str) -> str:
    code = getattr(error, "code", "")
    return str(code or fallback)
