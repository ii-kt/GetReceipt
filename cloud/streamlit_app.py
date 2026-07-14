from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from src.automation.browser_session import ManagedBrowser
from src.automation.credentials import credentials_configured, service_credentials
from src.automation.providers import build_receipt_fetcher
from src.config import (
    DATA_DIR,
    RECEIPT_DRIVE_FOLDER_URL,
    SERVICES,
    expected_transaction_month,
    month_label,
    selectable_months,
    service_by_id,
)
from src.storage.drive_storage import DriveStorage
from src.ui import styles as ui_styles
from src.workflows.auto_acquisition import run_auto_acquisition
from src.workflows.drive_status import StoredReceipt, find_receipt
from src.workflows.receipt_archive import (
    ReceiptArchive,
    archive_months,
    build_receipt_archive,
    duplicate_file_names,
    filter_receipts,
)


TOKYO = ZoneInfo("Asia/Tokyo")
BATCH_KEY = "getreceipt_batch"
FAILURE_KEY = "getreceipt_failure"
NOTICE_KEY = "getreceipt_notice"
LOGGER = logging.getLogger(__name__)
MONTHLY_VIEW = "毎月の4件"
ARCHIVE_VIEW = "単発領収書"


st.set_page_config(
    page_title="GetReceipt",
    page_icon=":material/receipt_long:",
    layout="centered",
    initial_sidebar_state="collapsed",
)


def drive_secrets_configured() -> bool:
    try:
        return "google_service_account" in st.secrets
    except Exception:
        return False


def current_sync_label() -> str:
    return f"Drive確認 {datetime.now(TOKYO):%H:%M}"


def load_drive_snapshot() -> tuple[DriveStorage | None, list[dict[str, str]], str]:
    if not drive_secrets_configured():
        return None, [], "Google Driveの接続情報がStreamlit Secretsにありません。"
    try:
        storage = DriveStorage.from_secrets(st.secrets)
        return storage, storage.list_files(), ""
    except Exception as error:
        return None, [], f"Google Driveの領収書フォルダを確認できませんでした: {error}"


def receipts_for_month(files: list[dict[str, str]], target_month: str) -> dict[str, StoredReceipt | None]:
    return {
        service.id: find_receipt(files, service, target_month)
        for service in SERVICES
    }


def failure_for(service_id: str, target_month: str) -> dict[str, str] | None:
    failure = st.session_state.get(FAILURE_KEY)
    if not isinstance(failure, dict):
        return None
    if failure.get("service_id") != service_id or failure.get("target_month") != target_month:
        return None
    return failure


def batch_for(target_month: str) -> dict[str, Any] | None:
    batch = st.session_state.get(BATCH_KEY)
    if not isinstance(batch, dict) or batch.get("target_month") != target_month:
        return None
    return batch


def render_service_rows(
    receipts: dict[str, StoredReceipt | None],
    target_month: str,
    batch: dict[str, Any] | None,
) -> None:
    pending = list(batch.get("service_ids", ())) if batch else []
    current_service = str(batch.get("current_service") or "") if batch else ""

    for service in SERVICES:
        receipt = receipts[service.id]
        transaction_month = expected_transaction_month(service.id, target_month)
        transaction_label = month_label(transaction_month).removesuffix("分")
        failure = failure_for(service.id, target_month) if receipt is None else None
        if receipt is not None:
            status = "saved"
            detail = f"{transaction_label}の取引PDFをDriveで確認済み"
        elif service.id == current_service:
            status = "running"
            detail = "自動取得を実行中"
        elif batch and service.id in pending:
            status = "queued"
            detail = "自動取得を待機中"
        elif failure:
            status = "failed"
            detail = failure.get("message", "自動取得に失敗しました。")
        else:
            status = "missing"
            detail = f"{transaction_label}の取引PDFはDriveにありません"

        ui_styles.render_service_card(
            label=service.label,
            eyebrow=service.default_partner,
            status=status,
            detail=detail,
            file_name=receipt.file_name if receipt else "",
            drive_url=receipt.web_view_link if receipt else "",
        )


def queue_batch(target_month: str, service_ids: list[str]) -> None:
    if not service_ids:
        return
    st.session_state[BATCH_KEY] = {
        "target_month": target_month,
        "service_ids": service_ids,
        "completed": [],
        "current_service": service_ids[0],
    }
    st.session_state.pop(FAILURE_KEY, None)
    st.session_state.pop(NOTICE_KEY, None)
    st.rerun()


def cleanup_runtime(browser: ManagedBrowser | None, run_dir: Path) -> str:
    errors: list[str] = []
    if browser is not None:
        try:
            browser.close(clear_profile=True)
        except Exception as error:
            errors.append(f"ブラウザを終了できませんでした: {error}")

    runtime_root = (DATA_DIR / "acquisition-runtime").resolve()
    target = run_dir.resolve()
    if target == runtime_root or runtime_root not in target.parents:
        errors.append("一時ファイルの削除対象が不正です。")
    elif target.exists():
        try:
            shutil.rmtree(target)
        except Exception as error:
            errors.append(f"一時ファイルを削除できませんでした: {error}")
    return " ".join(errors)


def execute_next_service(storage: DriveStorage, batch: dict[str, Any]) -> None:
    target_month = str(batch["target_month"])
    service_ids = list(batch.get("service_ids", ()))
    completed = list(batch.get("completed", ()))
    remaining = [service_id for service_id in service_ids if service_id not in completed]

    if not remaining:
        st.session_state[NOTICE_KEY] = "未取得だったPDFをすべてGoogle Driveで確認しました。"
        st.session_state.pop(BATCH_KEY, None)
        st.rerun()

    service_id = remaining[0]
    service = service_by_id(service_id)
    position = service_ids.index(service_id) + 1
    batch["current_service"] = service_id
    st.session_state[BATCH_KEY] = batch
    status_box = st.status(
        f"{position}/{len(service_ids)}  {service.label}を自動取得しています。",
        expanded=True,
    )

    run_dir = DATA_DIR / "acquisition-runtime" / f"{service_id}-{target_month}"
    browser: ManagedBrowser | None = None
    result = None
    unexpected_error = ""
    try:
        credentials = service_credentials(st.secrets, service_id)
        browser = ManagedBrowser(
            profile_dir=run_dir / "profile",
            download_dir=run_dir / "downloads",
        )
        fetcher = build_receipt_fetcher(service_id, browser, credentials)
        result = run_auto_acquisition(
            service_id=service_id,
            target_month=target_month,
            fetcher=fetcher,
            storage=storage,
            on_progress=lambda event: status_box.write(event.message),
        )
    except Exception as error:
        unexpected_error = f"自動取得を開始できませんでした: {error}"
    finally:
        cleanup_error = cleanup_runtime(browser, run_dir)
        if cleanup_error:
            LOGGER.error("Acquisition runtime cleanup failed: %s", cleanup_error)

    failure_code = ""
    failure_message = ""
    if unexpected_error:
        failure_code = "UNEXPECTED_ERROR"
        failure_message = unexpected_error
    elif result is None:
        failure_code = "ACQUISITION_FAILED"
        failure_message = "自動取得結果を確認できませんでした。"
    elif not result.success:
        failure = result.failure
        failure_code = failure.code if failure else "ACQUISITION_FAILED"
        failure_message = failure.message if failure else "自動取得に失敗しました。"
    if failure_code:
        st.session_state[FAILURE_KEY] = {
            "service_id": service_id,
            "target_month": target_month,
            "code": failure_code,
            "message": failure_message,
        }
        st.session_state.pop(BATCH_KEY, None)
        status_box.update(
            label=f"{service.label}の自動取得に失敗したため終了しました。",
            state="error",
        )
        st.rerun()

    completed.append(service_id)
    batch["completed"] = completed
    next_services = [item for item in service_ids if item not in completed]
    if next_services:
        batch["current_service"] = next_services[0]
        st.session_state[BATCH_KEY] = batch
        status_box.update(
            label=f"{service.label}をDriveで確認しました。次へ進みます。",
            state="complete",
        )
    else:
        st.session_state[NOTICE_KEY] = "未取得だったPDFをすべてGoogle Driveで確認しました。"
        st.session_state.pop(BATCH_KEY, None)
        status_box.update(label="自動取得が完了しました。", state="complete")

    st.rerun()


def render_monthly_view(
    storage: DriveStorage | None,
    drive_files: list[dict[str, str]],
    drive_error: str,
) -> None:
    months = list(reversed(selectable_months()))
    selected_month = st.selectbox(
        "利用月",
        months,
        format_func=month_label,
        key="getreceipt_month",
        label_visibility="collapsed",
        disabled=isinstance(st.session_state.get(BATCH_KEY), dict),
    )

    if drive_error or storage is None:
        st.session_state.pop(BATCH_KEY, None)
        ui_styles.render_month_hero(
            month_label=month_label(selected_month),
            saved_count=0,
            detail="Driveの保存状況を確認できません",
            state="failed",
        )
        ui_styles.render_fatal_notice(
            title="Google Driveへ接続できません",
            detail=drive_error,
            code="DRIVE_CONNECTION_FAILED",
        )
        st.stop()

    receipts = receipts_for_month(drive_files, selected_month)
    saved_count = sum(receipt is not None for receipt in receipts.values())
    missing_services = [service for service in SERVICES if receipts[service.id] is None]
    active_batch = batch_for(selected_month)

    stored_failure = st.session_state.get(FAILURE_KEY)
    if isinstance(stored_failure, dict):
        failed_service_id = str(stored_failure.get("service_id") or "")
        if (
            stored_failure.get("target_month") == selected_month
            and receipts.get(failed_service_id) is not None
        ):
            st.session_state.pop(FAILURE_KEY, None)

    if saved_count == len(SERVICES):
        hero_detail = "4件すべてのPDFをGoogle Driveで確認しました"
        hero_state = "complete"
    elif active_batch:
        hero_detail = "未取得PDFの自動取得を実行しています"
        hero_state = "running"
    else:
        hero_detail = f"未取得は{len(missing_services)}件です"
        hero_state = "ready"

    ui_styles.render_month_hero(
        month_label=month_label(selected_month),
        saved_count=saved_count,
        detail=hero_detail,
        state=hero_state,
    )
    ui_styles.render_progress(
        completed=saved_count,
        active_label=(
            service_by_id(str(active_batch.get("current_service"))).label
            if active_batch and active_batch.get("current_service")
            else ""
        ),
        state="running" if active_batch else ("complete" if saved_count == len(SERVICES) else "idle"),
    )

    notice = st.session_state.pop(NOTICE_KEY, "")
    if notice:
        st.success(notice, icon=":material/check_circle:")

    visible_failure = next(
        (
            failure_for(service.id, selected_month)
            for service in SERVICES
            if failure_for(service.id, selected_month)
        ),
        None,
    )
    missing_credentials = [
        service.label
        for service in missing_services
        if not credentials_configured(st.secrets, service.id)
    ]

    if visible_failure:
        ui_styles.render_fatal_notice(
            title="自動取得に失敗したため終了しました",
            detail=visible_failure.get("message", "自動取得に失敗しました。"),
            code=visible_failure.get("code", "ACQUISITION_FAILED"),
        )

    if not active_batch and missing_services and missing_credentials:
        ui_styles.render_fatal_notice(
            title="自動取得を開始できません",
            detail=f"ログイン情報が未設定です: {'、'.join(missing_credentials)}",
            code="CREDENTIALS_MISSING",
        )
    elif not active_batch and missing_services:
        retrying = visible_failure is not None
        if st.button(
            f"未取得{len(missing_services)}件を{'再度' if retrying else ''}自動取得",
            type="primary",
            use_container_width=True,
            icon=":material/download:",
        ):
            queue_batch(selected_month, [service.id for service in missing_services])

    render_service_rows(receipts, selected_month, active_batch)
    st.link_button(
        "Google Driveの領収書フォルダを開く",
        RECEIPT_DRIVE_FOLDER_URL,
        use_container_width=True,
        icon=":material/folder_open:",
    )

    if active_batch:
        execute_next_service(storage, active_batch)


def _currency_code(value: object) -> str:
    return str(getattr(value, "value", value))


def _currency_label(value: object) -> str:
    code = _currency_code(value)
    return {
        "JPY": "JPY / 円",
        "USD": "USD / $",
        "EUR": "EUR / €",
        "GBP": "GBP / £",
    }.get(code, code)


def render_archive_view(drive_files: list[dict[str, str]], drive_error: str) -> None:
    if drive_error:
        ui_styles.render_archive_hero(
            total_count=0,
            visible_count=0,
            refund_count=0,
        )
        ui_styles.render_fatal_notice(
            title="Google Driveへ接続できません",
            detail=drive_error,
            code="DRIVE_CONNECTION_FAILED",
        )
        st.stop()

    archive: ReceiptArchive = build_receipt_archive(drive_files)
    all_receipts = archive.receipts
    query = st.text_input(
        "単発領収書を検索",
        placeholder="請求元・金額・ファイル名",
        key="archive_query",
    )
    currency_codes = sorted(
        {_currency_code(receipt.currency) for receipt in all_receipts},
        key=lambda code: ({"JPY": 0, "USD": 1, "EUR": 2, "GBP": 3}.get(code, 9), code),
    )
    filter_columns = st.columns(3, gap="small")
    with filter_columns[0]:
        month_filter = st.selectbox(
            "取引月",
            ("", *archive_months(all_receipts)),
            format_func=lambda value: (
                "すべての取引月" if not value else month_label(value).removesuffix("分")
            ),
            key="archive_month",
        )
    with filter_columns[1]:
        currency_filter = st.selectbox(
            "通貨",
            ("", *currency_codes),
            format_func=lambda value: "すべての通貨" if not value else _currency_label(value),
            key="archive_currency",
        )
    with filter_columns[2]:
        refund_filter = st.selectbox(
            "取引種別",
            ("", "standard", "refund"),
            format_func=lambda value: {
                "": "通常・返金すべて",
                "standard": "通常",
                "refund": "返金あり",
            }[value],
            key="archive_refund",
        )
    visible_receipts = filter_receipts(
        all_receipts,
        query=query,
        month=month_filter,
        currency=currency_filter,
        refund=refund_filter,
    )
    visible_refunds = sum(receipt.is_refund for receipt in visible_receipts)
    ui_styles.render_archive_hero(
        total_count=len(all_receipts),
        visible_count=len(visible_receipts),
        refund_count=visible_refunds,
        review_count=len(archive.review_files),
    )

    if visible_receipts:
        by_month: dict[str, list[Any]] = defaultdict(list)
        for receipt in visible_receipts:
            by_month[receipt.transaction_month].append(receipt)
        duplicates = set(duplicate_file_names(all_receipts))
        for transaction_month in sorted(by_month, reverse=True):
            month_receipts = by_month[transaction_month]
            ui_styles.render_archive_section(
                month_label=month_label(transaction_month).removesuffix("分"),
                count=len(month_receipts),
            )
            for receipt in month_receipts:
                ui_styles.render_one_off_receipt_card(
                    transaction_date=receipt.transaction_date.strftime("%Y.%m.%d"),
                    partner_name=receipt.partner_name,
                    amount_label=receipt.amount_label,
                    currency_label=_currency_label(receipt.currency),
                    has_refund=receipt.is_refund,
                    file_name=receipt.file_name,
                    drive_url=receipt.web_view_link,
                    duplicate=receipt.file_name in duplicates,
                )
    else:
        ui_styles.render_archive_empty(
            title="該当する単発領収書はありません",
            detail="検索条件を変更すると、Drive上の別の領収書を確認できます。",
        )

    if archive.review_files:
        with st.expander(f"要確認のPDF  {len(archive.review_files)}件", expanded=False):
            for file in archive.review_files:
                ui_styles.render_review_file(
                    file_name=file.file_name,
                    reason=file.reason,
                    drive_url=file.web_view_link,
                )

    st.link_button(
        "Google Driveの領収書フォルダを開く",
        RECEIPT_DRIVE_FOLDER_URL,
        use_container_width=True,
        icon=":material/folder_open:",
    )


ui_styles.inject_design()
storage, drive_files, drive_error = load_drive_snapshot()
ui_styles.render_compact_header(
    sync_label=current_sync_label() if not drive_error else "Drive未確認",
    drive_url=RECEIPT_DRIVE_FOLDER_URL,
)

batch_active = isinstance(st.session_state.get(BATCH_KEY), dict)
if batch_active:
    st.session_state["getreceipt_view"] = MONTHLY_VIEW
view_default = None if "getreceipt_view" in st.session_state else MONTHLY_VIEW
selected_view = st.segmented_control(
    "表示",
    (MONTHLY_VIEW, ARCHIVE_VIEW),
    default=view_default,
    selection_mode="single",
    key="getreceipt_view",
    label_visibility="collapsed",
    disabled=batch_active,
    width="stretch",
)

if selected_view == ARCHIVE_VIEW:
    render_archive_view(drive_files, drive_error)
else:
    render_monthly_view(storage, drive_files, drive_error)
