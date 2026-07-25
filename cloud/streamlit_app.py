from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from src.automation.browser_session import ManagedBrowser, find_browser_executable
from src.automation import security_challenge as challenge_runtime
from src.automation.auth_challenges import profile_for
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
from src.jobs.client import (
    WorkerApiError,
    WorkerClient,
    WorkerConfigError,
    worker_connection_from_secrets,
)
from src.storage.drive_storage import DriveStorage
from src.ui import remote_jobs
from src.ui.access_control import require_owner_access
from src.ui.graph_link import graph_manager_from_secrets, render_graph_connection
from src.ui.manual_upload import render_manual_upload
from src.ui import styles as ui_styles
from src.ui.module_contract import ensure_ui_module
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
FAILURES_KEY = "getreceipt_service_failures"

# The Streamlit control plane may fall back to Chromium when Google Chrome
# cannot be installed (Streamlit Community Cloud lite mode). The persistent
# worker never imports this module and stays Chrome-Stable-only.
os.environ.setdefault("GETRECEIPT_ALLOW_CHROMIUM", "1")
NOTICE_KEY = "getreceipt_notice"
SECURITY_CHALLENGE_KEY = "getreceipt_security_challenge"
SECURITY_WAITING_PHASE = "awaiting_security_code"
SECURITY_SUBMITTING_PHASE = "submitting_security_code"
LOGGER = logging.getLogger(__name__)
MONTHLY_VIEW = "毎月の4件"
ARCHIVE_VIEW = "単発領収書"
EXPECTED_UI_API_VERSION = 2
REQUIRED_UI_CALLABLES = (
    "inject_design",
    "render_compact_header",
    "render_month_hero",
    "render_progress",
    "render_service_card",
    "render_fatal_notice",
    "render_archive_hero",
    "render_archive_section",
    "render_one_off_receipt_card",
    "render_archive_empty",
    "render_review_file",
)

ui_styles = ensure_ui_module(
    ui_styles,
    expected_version=EXPECTED_UI_API_VERSION,
    required_callables=REQUIRED_UI_CALLABLES,
)


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
    except Exception:
        return None, [], "Google Driveの領収書フォルダを確認できませんでした。"


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


def security_challenge_for(target_month: str) -> dict[str, Any] | None:
    challenge = st.session_state.get(SECURITY_CHALLENGE_KEY)
    if not isinstance(challenge, dict) or challenge.get("target_month") != target_month:
        return None
    return challenge


def discard_security_challenge() -> None:
    challenge = st.session_state.pop(SECURITY_CHALLENGE_KEY, None)
    if not isinstance(challenge, dict):
        return
    token = str(challenge.get("token") or "")
    if not token:
        return
    try:
        challenge_runtime.browser_lease_registry.discard(token)
    except Exception as error:
        LOGGER.error(
            "Security challenge runtime cleanup failed (%s)",
            type(error).__name__,
        )


def render_service_rows(
    receipts: dict[str, StoredReceipt | None],
    target_month: str,
    batch: dict[str, Any] | None,
) -> None:
    pending = list(batch.get("service_ids", ())) if batch else []
    completed = set(batch.get("completed", ())) if batch else set()
    current_service = str(batch.get("current_service") or "") if batch else ""
    remote_failures = batch.get("failed") if batch else {}
    if not isinstance(remote_failures, dict):
        remote_failures = {}
    challenge = security_challenge_for(target_month)
    challenge_service = str(challenge.get("service_id") or "") if challenge else ""
    persisted_failures = st.session_state.get(FAILURES_KEY, {})
    month_failures = (
        persisted_failures.get(target_month, {})
        if isinstance(persisted_failures, dict)
        else {}
    )

    for service in SERVICES:
        receipt = receipts[service.id]
        transaction_month = expected_transaction_month(service.id, target_month)
        transaction_label = month_label(transaction_month).removesuffix("分")
        remote_failure = remote_failures.get(service.id)
        if not isinstance(remote_failure, dict):
            remote_failure = None
        if remote_failure is None and receipt is None:
            stored = month_failures.get(service.id)
            if isinstance(stored, dict):
                remote_failure = stored
        local_failure = (
            failure_for(service.id, target_month)
            if receipt is None
            else None
        )
        if receipt is not None:
            status = "saved"
            detail = f"{transaction_label}の取引PDFをDriveで確認済み"
        elif service.id in completed:
            status = "running"
            detail = "保存完了。Google Driveの表示を更新中"
        elif remote_failure:
            status = "failed"
            reason = str(remote_failure.get("detail") or "")
            code = str(remote_failure.get("code") or "")
            base = remote_failure.get("message", "自動取得に失敗しました。")
            detail = base
            if code:
                detail = f"{base}（{code}）"
            if reason:
                detail = f"{detail} {reason}"
        elif service.id == challenge_service:
            status = "running"
            detail = "本人確認コードの入力を待っています"
        elif service.id == current_service:
            status = "running"
            detail = "自動取得を実行中"
        elif batch and service.id in pending:
            status = "queued"
            detail = "自動取得を待機中"
        elif local_failure:
            status = "failed"
            detail = local_failure.get("message", "自動取得に失敗しました。")
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
    discard_security_challenge()
    st.session_state[BATCH_KEY] = {
        "target_month": target_month,
        "service_ids": service_ids,
        "completed": [],
        "failed": {},
        "current_service": service_ids[0],
    }
    st.session_state.pop(FAILURE_KEY, None)
    st.session_state.pop(NOTICE_KEY, None)
    # Clear the previous run's failures for the services being retried.
    persisted = dict(st.session_state.get(FAILURES_KEY, {}))
    month_failures = dict(persisted.get(target_month, {}))
    for service_id in service_ids:
        month_failures.pop(service_id, None)
    persisted[target_month] = month_failures
    st.session_state[FAILURES_KEY] = persisted
    st.rerun()


def cleanup_runtime(browser: ManagedBrowser | None, run_dir: Path) -> str:
    errors: list[str] = []
    if browser is not None:
        try:
            browser.close(clear_profile=True)
        except Exception:
            errors.append("browser")

    runtime_root = (DATA_DIR / "acquisition-runtime").resolve()
    target = run_dir.resolve()
    if target == runtime_root or runtime_root not in target.parents:
        errors.append("一時ファイルの削除対象が不正です。")
    elif target.exists():
        try:
            shutil.rmtree(target)
        except Exception:
            errors.append("runtime_files")
    return ",".join(errors)


def _finish_batch(batch: dict[str, Any]) -> None:
    failed = dict(batch.get("failed", {}))
    if failed:
        labels = "、".join(
            service_by_id(service_id).label for service_id in failed
        )
        st.session_state[NOTICE_KEY] = (
            f"{labels}は自動取得できませんでした。他のサービスは完了しています。"
            "失敗した分はもう一度実行するか、手動でPDFを追加できます。"
        )
    else:
        st.session_state[NOTICE_KEY] = "未取得だったPDFをすべてGoogle Driveで確認しました。"
    st.session_state.pop(BATCH_KEY, None)


def _advance_batch(batch: dict[str, Any]) -> bool:
    """Move to the next pending service; report True when the batch ended."""

    service_ids = list(batch.get("service_ids", ()))
    completed = list(batch.get("completed", ()))
    failed = dict(batch.get("failed", {}))
    next_services = [
        item
        for item in service_ids
        if item not in completed and item not in failed
    ]
    if next_services:
        batch["current_service"] = next_services[0]
        batch["phase"] = "running"
        st.session_state[BATCH_KEY] = batch
        return False
    _finish_batch(batch)
    return True


def fail_batch_service(
    batch: dict[str, Any],
    service_id: str,
    *,
    code: str,
    message: str,
    detail: str = "",
) -> bool:
    """Record one service failure and continue with the remaining services."""

    failed = dict(batch.get("failed", {}))
    failed[service_id] = {"code": code, "message": message, "detail": detail}
    batch["failed"] = failed
    # Persist beyond the batch so the reason stays visible after completion.
    target_month = str(batch.get("target_month") or "")
    persisted = dict(st.session_state.get(FAILURES_KEY, {}))
    month_failures = dict(persisted.get(target_month, {}))
    month_failures[service_id] = {
        "code": code,
        "message": message,
        "detail": detail,
    }
    persisted[target_month] = month_failures
    st.session_state[FAILURES_KEY] = persisted
    return _advance_batch(batch)


def complete_batch_service(batch: dict[str, Any], service_id: str) -> bool:
    completed = list(batch.get("completed", ()))
    if service_id not in completed:
        completed.append(service_id)
    batch["completed"] = completed
    return _advance_batch(batch)


def security_challenge_state_matches(
    batch: dict[str, Any],
    challenge: dict[str, Any],
    *,
    phase: str,
) -> bool:
    """Confirm that a security-code action still owns the live session state."""

    current_batch = st.session_state.get(BATCH_KEY)
    current_challenge = st.session_state.get(SECURITY_CHALLENGE_KEY)
    if not isinstance(current_batch, dict) or not isinstance(current_challenge, dict):
        return False
    if current_batch.get("phase") != phase or current_challenge.get("phase") != phase:
        return False

    token = str(challenge.get("token") or "")
    service_id = str(challenge.get("service_id") or "")
    target_month = str(challenge.get("target_month") or "")
    if not token or not service_id or not target_month:
        return False

    return (
        str(current_challenge.get("token") or "") == token
        and str(current_challenge.get("service_id") or "") == service_id
        and str(current_challenge.get("target_month") or "") == target_month
        and str(current_batch.get("current_service") or "") == service_id
        and str(current_batch.get("target_month") or "") == target_month
        and str(batch.get("current_service") or "") == service_id
        and str(batch.get("target_month") or "") == target_month
    )


def begin_security_code_submission(
    batch: dict[str, Any],
    challenge: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Move both state records to submitting without retaining the OTP value."""

    if not security_challenge_state_matches(
        batch,
        challenge,
        phase=SECURITY_WAITING_PHASE,
    ):
        return None

    current_batch = st.session_state[BATCH_KEY]
    current_challenge = st.session_state[SECURITY_CHALLENGE_KEY]
    submitting_batch = {**current_batch, "phase": SECURITY_SUBMITTING_PHASE}
    submitting_challenge = {**current_challenge, "phase": SECURITY_SUBMITTING_PHASE}

    # There is no Streamlit call between these assignments, so another rerun
    # cannot observe a clickable waiting form after only one record changed.
    st.session_state[BATCH_KEY] = submitting_batch
    st.session_state[SECURITY_CHALLENGE_KEY] = submitting_challenge
    return submitting_batch, submitting_challenge


def restore_security_code_waiting(
    batch: dict[str, Any],
    challenge: dict[str, Any],
    *,
    error: str,
) -> bool:
    """Return a rejected provider response to the same safe waiting form."""

    if not security_challenge_state_matches(
        batch,
        challenge,
        phase=SECURITY_SUBMITTING_PHASE,
    ):
        return False
    waiting_batch = {**st.session_state[BATCH_KEY], "phase": SECURITY_WAITING_PHASE}
    waiting_challenge = {
        **st.session_state[SECURITY_CHALLENGE_KEY],
        "phase": SECURITY_WAITING_PHASE,
        "error": error,
    }
    st.session_state[BATCH_KEY] = waiting_batch
    st.session_state[SECURITY_CHALLENGE_KEY] = waiting_challenge
    return True


def _run_tokuten_via_graph(
    *,
    storage: DriveStorage,
    batch: dict[str, Any],
    service: Any,
    target_month: str,
    graph_manager: Any,
    status_box: Any,
) -> None:
    """Fetch the electricity receipt through Microsoft Graph (no browser)."""

    from src.automation.microsoft_graph import TokutenGraphFetcher

    result = None
    unexpected_error = ""
    try:
        fetcher = TokutenGraphFetcher(graph_manager.access_token)
        result = run_auto_acquisition(
            service_id="tokuten",
            target_month=target_month,
            fetcher=fetcher,
            storage=storage,
            on_progress=lambda event: status_box.write(event.message),
        )
    except Exception as error:
        unexpected_error = f"{type(error).__name__}: {error}"[:300]
        LOGGER.warning("Tokuten Graph acquisition failed (%s)", unexpected_error)

    if unexpected_error:
        failure_code = "MICROSOFT_GRAPH_FAILED"
        failure_message = "Microsoftメールからの取得に失敗しました。"
        failure_detail = unexpected_error
    elif result is None or not result.success:
        failure = getattr(result, "failure", None)
        failure_code = getattr(failure, "code", "") or "MICROSOFT_GRAPH_FAILED"
        failure_message = getattr(failure, "message", "") or "Microsoftメールからの取得に失敗しました。"
        failure_detail = str(getattr(failure, "detail", "") or "")
    else:
        failure_code = ""
        failure_message = ""
        failure_detail = ""

    if failure_code:
        batch_complete = fail_batch_service(
            batch,
            "tokuten",
            code=failure_code,
            message=failure_message,
            detail=failure_detail,
        )
        status_box.update(
            label=(
                f"{service.label}の自動取得に失敗しました。"
                + ("" if batch_complete else "残りのサービスへ進みます。")
            ),
            state="error",
        )
        st.rerun()

    batch_complete = complete_batch_service(batch, "tokuten")
    status_box.update(
        label=(
            "自動取得が完了しました。"
            if batch_complete
            else f"{service.label}をDriveで確認しました。次へ進みます。"
        ),
        state="complete",
    )
    st.rerun()


def execute_next_service(storage: DriveStorage, batch: dict[str, Any]) -> None:
    target_month = str(batch["target_month"])
    service_ids = list(batch.get("service_ids", ()))
    completed = list(batch.get("completed", ()))
    already_failed = dict(batch.get("failed", {}))
    remaining = [
        service_id
        for service_id in service_ids
        if service_id not in completed and service_id not in already_failed
    ]

    if not remaining:
        _finish_batch(batch)
        st.rerun()

    service_id = remaining[0]
    service = service_by_id(service_id)
    position = service_ids.index(service_id) + 1
    batch["current_service"] = service_id
    batch["phase"] = "running"
    st.session_state[BATCH_KEY] = batch
    status_box = st.status(
        f"{position}/{len(service_ids)}  {service.label}を自動取得しています。",
        expanded=True,
    )

    if service_id == "tokuten":
        graph_manager = graph_manager_from_secrets(st.secrets, storage)
        if graph_manager is not None:
            try:
                connected = bool(graph_manager.status().get("connected"))
            except Exception:
                connected = False
            if connected:
                _run_tokuten_via_graph(
                    storage=storage,
                    batch=batch,
                    service=service,
                    target_month=target_month,
                    graph_manager=graph_manager,
                    status_box=status_box,
                )
                return

    run_dir = challenge_runtime.new_attempt_run_dir(service_id, target_month)
    browser: ManagedBrowser | None = None
    result = None
    unexpected_error = ""
    runtime_preserved = False
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
        if getattr(result, "action_required", False):
            if not callable(getattr(fetcher, "resume_after_security_code", None)):
                raise RuntimeError("この請求元は確認コードによる安全な再開に対応していません。")
            challenge = getattr(result, "challenge", None)
            profile_id = "webbilling" if service_id == "mobile" else service_id
            profile = profile_for(profile_id)
            input_label = {
                "epos": "カード記載の3桁セキュリティコード",
                "commufa": "メールに届いた確認コード",
                "mobile": "メールまたはSMSに届いた確認コード",
            }.get(service_id, "確認コード")
            ticket = challenge_runtime.browser_lease_registry.create(
                service_id=service_id,
                target_month=target_month,
                browser=browser,
                run_dir=run_dir,
            )
            st.session_state[SECURITY_CHALLENGE_KEY] = {
                "token": ticket.token,
                "service_id": service_id,
                "target_month": target_month,
                "kind": str(getattr(challenge, "kind", "verification_code")),
                "message": str(getattr(challenge, "message", "確認コードの入力が必要です。")),
                "expires_at": ticket.expires_at,
                "input_label": input_label,
                "min_length": profile.min_length,
                "max_length": profile.max_length,
                "pattern": profile.pattern.pattern,
                "error": "",
            }
            batch["phase"] = "awaiting_security_code"
            st.session_state[BATCH_KEY] = batch
            runtime_preserved = True
    except Exception:
        unexpected_error = (
            "自動取得を開始できませんでした。時間をおいて再試行してください。"
        )
    finally:
        if not runtime_preserved:
            cleanup_error = cleanup_runtime(browser, run_dir)
            if cleanup_error:
                LOGGER.error(
                    "Acquisition runtime cleanup failed (%s)",
                    cleanup_error,
                )

    if runtime_preserved:
        status_box.update(
            label=f"{service.label}は本人確認コードの入力を待っています。",
            state="running",
        )
        st.rerun()

    failure_code = ""
    failure_message = ""
    failure_detail = ""
    if unexpected_error:
        failure_code = "UNEXPECTED_ERROR"
        failure_message = unexpected_error
        failure_detail = unexpected_error
    elif result is None:
        failure_code = "ACQUISITION_FAILED"
        failure_message = "自動取得結果を確認できませんでした。"
    elif not result.success:
        failure = result.failure
        failure_code = failure.code if failure else "ACQUISITION_FAILED"
        failure_message = failure.message if failure else "自動取得に失敗しました。"
        failure_detail = str(getattr(failure, "detail", "") or "")
    if failure_code:
        batch_complete = fail_batch_service(
            batch,
            service_id,
            code=failure_code,
            message=failure_message,
            detail=failure_detail,
        )
        status_box.update(
            label=(
                f"{service.label}の自動取得に失敗しました。"
                + ("" if batch_complete else "残りのサービスへ進みます。")
            ),
            state="error",
        )
        st.rerun()

    batch_complete = complete_batch_service(batch, service_id)
    if not batch_complete:
        status_box.update(
            label=f"{service.label}をDriveで確認しました。次へ進みます。",
            state="complete",
        )
    else:
        status_box.update(label="自動取得が完了しました。", state="complete")

    st.rerun()


def resume_security_code(
    storage: DriveStorage,
    batch: dict[str, Any],
    challenge: dict[str, Any],
    code: str,
) -> None:
    token = str(challenge.get("token") or "")
    service_id = str(challenge.get("service_id") or "")
    target_month = str(challenge.get("target_month") or "")
    service = service_by_id(service_id)
    result = None
    unexpected_error = ""
    resume_detail = ""
    status_box = st.status(
        f"{service.label}へ確認コードを送信し、自動取得を再開しています。",
        expanded=True,
    )

    try:
        with challenge_runtime.browser_lease_registry.checkout(
            token,
            expected_service_id=service_id,
            expected_target_month=target_month,
        ) as lease:
            credentials = service_credentials(st.secrets, service_id)
            fetcher = build_receipt_fetcher(service_id, lease.browser, credentials)
            resume = getattr(fetcher, "resume_after_security_code", None)
            if not callable(resume):
                raise RuntimeError("この請求元は確認コードによる再開に対応していません。")
            result = run_auto_acquisition(
                service_id=service_id,
                target_month=target_month,
                fetcher=fetcher,
                storage=storage,
                on_progress=lambda event: status_box.write(event.message),
                fetch_statement=lambda month: resume(month, code),
            )
    except challenge_runtime.BrowserLeaseUnavailableError:
        st.session_state.pop(SECURITY_CHALLENGE_KEY, None)
        batch["phase"] = "running"
        st.session_state[BATCH_KEY] = batch
        st.session_state[NOTICE_KEY] = (
            "確認コード待機セッションの期限が切れたため、新しい確認コードを発行します。"
        )
        status_box.update(label="新しい確認コードを発行します。", state="running")
        st.rerun()
    except Exception as error:
        unexpected_error = (
            "確認コード送信後の自動取得を再開できませんでした。"
            "新しい確認コードで再試行してください。"
        )
        resume_detail = f"{type(error).__name__}: {error}"[:300]
        LOGGER.warning("Security code resume failed (%s)", resume_detail)

    if result is not None and getattr(result, "action_required", False):
        updated = dict(challenge)
        minimum = int(challenge.get("min_length") or 6)
        maximum = int(challenge.get("max_length") or 6)
        expected_label = (
            f"{maximum}桁コード"
            if minimum == maximum
            else f"{minimum}〜{maximum}桁コード"
        )
        updated["error"] = (
            f"確認コードを受け付けられませんでした。最新の{expected_label}を確認してください。"
        )
        st.session_state[SECURITY_CHALLENGE_KEY] = updated
        status_box.update(label="確認コードを再確認してください。", state="error")
        st.rerun()

    challenge_runtime.browser_lease_registry.discard(token)
    st.session_state.pop(SECURITY_CHALLENGE_KEY, None)

    failure_code = ""
    failure_message = ""
    failure_detail = ""
    if unexpected_error:
        failure_code = "SECURITY_CODE_RESUME_FAILED"
        failure_message = unexpected_error
        failure_detail = resume_detail
    elif result is None:
        failure_code = "SECURITY_CODE_RESUME_FAILED"
        failure_message = "確認コード送信後の自動取得結果を確認できませんでした。"
    elif not result.success:
        failure = result.failure
        failure_code = failure.code if failure else "ACQUISITION_FAILED"
        failure_message = failure.message if failure else "自動取得に失敗しました。"
        failure_detail = str(getattr(failure, "detail", "") or "")

    if failure_code:
        batch_complete = fail_batch_service(
            batch,
            service_id,
            code=failure_code,
            message=failure_message,
            detail=failure_detail,
        )
        status_box.update(
            label=(
                f"{service.label}の自動取得に失敗しました。"
                + ("" if batch_complete else "残りのサービスへ進みます。")
            ),
            state="error",
        )
        st.rerun()

    batch_complete = complete_batch_service(batch, service_id)
    status_box.update(
        label=(
            "自動取得が完了しました。"
            if batch_complete
            else f"{service.label}をDriveで確認しました。次へ進みます。"
        ),
        state="complete",
    )
    st.rerun()


def restart_security_challenge(batch: dict[str, Any], challenge: dict[str, Any]) -> None:
    token = str(challenge.get("token") or "")
    if token:
        challenge_runtime.browser_lease_registry.discard(token)
    st.session_state.pop(SECURITY_CHALLENGE_KEY, None)
    batch["phase"] = "running"
    st.session_state[BATCH_KEY] = batch
    st.session_state[NOTICE_KEY] = "新しい確認コードを発行するため、ログインから再開します。"
    st.rerun()


def render_security_code_form(
    storage: DriveStorage,
    batch: dict[str, Any],
    challenge: dict[str, Any],
) -> None:
    token = str(challenge.get("token") or "")
    service_id = str(challenge.get("service_id") or "")
    target_month = str(challenge.get("target_month") or "")
    service = service_by_id(service_id)
    try:
        metadata = challenge_runtime.browser_lease_registry.metadata(token)
    except challenge_runtime.BrowserLeaseUnavailableError:
        st.session_state.pop(SECURITY_CHALLENGE_KEY, None)
        batch["phase"] = "running"
        st.session_state[BATCH_KEY] = batch
        st.session_state[NOTICE_KEY] = (
            "確認コード待機セッションの期限が切れたため、新しい確認コードを発行します。"
        )
        st.rerun()

    if metadata.service_id != service_id or metadata.target_month != target_month:
        restart_security_challenge(batch, challenge)

    expires_label = metadata.expires_at.astimezone(TOKYO).strftime("%H:%M")
    message = str(challenge.get("message") or "追加の本人確認コードが必要です。")
    st.warning(
        f"{service.label}: {message}"
        " iPhoneで確認し、この画面へ戻って入力してください。自動取得は終了せず待機しています。",
        icon=":material/mark_email_unread:",
    )
    st.caption(f"入力期限の目安: {expires_label}　このタブは閉じたり再読み込みしたりしないでください。")
    error_message = str(challenge.get("error") or "")
    if error_message:
        st.error(error_message, icon=":material/error:")

    with st.form("security_code_form", clear_on_submit=True, border=True):
        minimum = int(challenge.get("min_length") or 6)
        maximum = int(challenge.get("max_length") or 6)
        input_label = str(challenge.get("input_label") or "確認コード")
        placeholder = (
            f"{minimum}桁の数字"
            if minimum == maximum
            else f"{minimum}〜{maximum}桁の数字"
        )
        code = st.text_input(
            input_label,
            type="password",
            max_chars=maximum,
            autocomplete="one-time-code",
            placeholder=placeholder,
        )
        submitted = st.form_submit_button(
            "認証して自動取得を続行",
            type="primary",
            use_container_width=True,
            icon=":material/verified_user:",
        )

    if submitted:
        normalized_code = unicodedata.normalize("NFKC", str(code or "")).strip()
        pattern = str(challenge.get("pattern") or r"^[0-9]{6}$")
        if re.fullmatch(pattern, normalized_code) is None:
            updated = dict(challenge)
            updated["error"] = (
                f"確認コードは{placeholder}で入力してください。"
            )
            st.session_state[SECURITY_CHALLENGE_KEY] = updated
            st.rerun()
        resume_security_code(storage, batch, challenge, normalized_code)

    if st.button(
        "新しい確認コードを発行",
        use_container_width=True,
        icon=":material/refresh:",
    ):
        restart_security_challenge(batch, challenge)


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
        discard_security_challenge()
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

    try:
        worker_connection = worker_connection_from_secrets(st.secrets)
    except WorkerConfigError as error:
        ui_styles.render_month_hero(
            month_label=month_label(selected_month),
            saved_count=saved_count,
            detail="常設ワーカーの設定を確認してください",
            state="failed",
        )
        ui_styles.render_fatal_notice(
            title="iPhone用ワーカーを開始できません",
            detail=str(error),
            code="WORKER_CONFIG_INVALID",
        )
        render_service_rows(receipts, selected_month, None)
        render_manual_upload(
            storage=storage,
            target_month=selected_month,
            missing_services=missing_services,
        )
        return

    if worker_connection is not None:
        worker_client = WorkerClient(worker_connection)
        microsoft_required = any(
            service.id == "tokuten"
            for service in missing_services
        )
        microsoft_ready = remote_jobs.render_microsoft_connection(
            worker_client,
            required=microsoft_required,
        )
        acquirable_service_ids = [
            service.id
            for service in missing_services
            if service.id != "tokuten" or microsoft_ready
        ]
        remote_jobs.render_remote_month(
            client=worker_client,
            target_month=selected_month,
            receipts=receipts,
            missing_service_ids=[service.id for service in missing_services],
            acquirable_service_ids=acquirable_service_ids,
            render_hero=lambda **values: ui_styles.render_month_hero(
                month_label=month_label(selected_month),
                **values,
            ),
            render_progress=ui_styles.render_progress,
            render_rows=render_service_rows,
        )
        render_manual_upload(
            storage=storage,
            target_month=selected_month,
            missing_services=missing_services,
            acquisition_active=lambda: _remote_acquisition_active(
                worker_client,
                selected_month,
            ),
            worker_client=worker_client,
        )
        st.link_button(
            "Google Driveの領収書フォルダを開く",
            RECEIPT_DRIVE_FOLDER_URL,
            use_container_width=True,
            icon=":material/folder_open:",
        )
        return

    if missing_services and find_browser_executable() is None:
        ui_styles.render_month_hero(
            month_label=month_label(selected_month),
            saved_count=saved_count,
            detail="常設のGoogle Chromeワーカーが未設定です",
            state="failed",
        )
        ui_styles.render_fatal_notice(
            title="自動取得はワーカー配備後に使えます",
            detail=(
                "お使いの端末のChromeは関係ありません。自動取得は、"
                "クラウド上の常設ワーカー(VM)内のGoogle Chromeで動作します。"
                "ワーカーを配備し、Streamlit Secretsの[receipt_worker]を"
                "設定すると有効になります。それまでも下の手動PDF追加と"
                "Drive確認は利用できます。"
            ),
            code="CHROME_WORKER_NOT_CONFIGURED",
        )
        render_service_rows(receipts, selected_month, None)
        render_manual_upload(
            storage=storage,
            target_month=selected_month,
            missing_services=missing_services,
        )
        st.link_button(
            "Google Driveの領収書フォルダを開く",
            RECEIPT_DRIVE_FOLDER_URL,
            use_container_width=True,
            icon=":material/folder_open:",
        )
        return

    active_batch = batch_for(selected_month)
    active_challenge = security_challenge_for(selected_month)
    if active_challenge and not active_batch:
        discard_security_challenge()
        active_challenge = None
    elif active_challenge and active_batch:
        challenged_service_id = str(active_challenge.get("service_id") or "")
        if receipts.get(challenged_service_id) is not None:
            discard_security_challenge()
            complete_batch_service(active_batch, challenged_service_id)
            st.rerun()

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
    elif active_challenge:
        challenge_service = service_by_id(str(active_challenge.get("service_id") or ""))
        hero_detail = f"{challenge_service.label}の本人確認コードを待っています"
        hero_state = "running"
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

    # Electricity (Tokuten) is fetched through Microsoft Graph, not a browser,
    # so it needs a one-time Microsoft mail connection instead of a login page.
    graph_manager = graph_manager_from_secrets(st.secrets, storage)
    tokuten_missing = any(service.id == "tokuten" for service in missing_services)
    graph_connected = False
    if graph_manager is not None and tokuten_missing and not active_batch:
        graph_connected = render_graph_connection(
            st, graph_manager, required=True
        )

    def _acquirable(service_id: str) -> bool:
        if service_id == "tokuten" and graph_manager is not None:
            return graph_connected
        return True

    queueable = [
        service.id for service in missing_services if _acquirable(service.id)
    ]

    if active_batch and active_challenge:
        render_security_code_form(storage, active_batch, active_challenge)
    elif not active_batch and missing_services and missing_credentials and not queueable:
        ui_styles.render_fatal_notice(
            title="自動取得を開始できません",
            detail=f"ログイン情報が未設定です: {'、'.join(missing_credentials)}",
            code="CREDENTIALS_MISSING",
        )
    elif not active_batch and queueable:
        retrying = visible_failure is not None
        if st.button(
            f"未取得{len(queueable)}件を{'再度' if retrying else ''}自動取得",
            type="primary",
            use_container_width=True,
            icon=":material/download:",
        ):
            queue_batch(selected_month, queueable)

    render_service_rows(receipts, selected_month, active_batch)
    render_manual_upload(
        storage=storage,
        target_month=selected_month,
        missing_services=missing_services,
        acquisition_active=lambda: active_batch is not None,
    )
    st.link_button(
        "Google Driveの領収書フォルダを開く",
        RECEIPT_DRIVE_FOLDER_URL,
        use_container_width=True,
        icon=":material/folder_open:",
    )

    if active_batch and not active_challenge:
        execute_next_service(storage, active_batch)


def _remote_acquisition_active(client: WorkerClient, target_month: str) -> bool:
    try:
        return client.find_active_job(target_month) is not None
    except Exception:
        # A connectivity failure must not permit a concurrent manual save.
        return True


def handle_microsoft_oauth_callback(storage: DriveStorage | None = None) -> None:
    notice = str(st.session_state.pop("microsoft_oauth_notice", "") or "")
    oauth_error = str(st.session_state.pop("microsoft_oauth_error", "") or "")
    if notice:
        st.success(notice, icon=":material/mark_email_read:")
    if oauth_error:
        st.error(oauth_error, icon=":material/mail_lock:")

    code = _query_value("code")
    state = _query_value("state")
    provider_error = _query_value("error")
    if not state or (not code and not provider_error):
        return

    try:
        connection = worker_connection_from_secrets(st.secrets)
    except WorkerConfigError:
        connection = None

    graph_manager = None
    if connection is None:
        graph_manager = graph_manager_from_secrets(st.secrets, storage)
        if graph_manager is None:
            return

    if provider_error:
        st.session_state["microsoft_oauth_error"] = (
            "Microsoftメール接続がキャンセルされたか、認証に失敗しました。"
        )
        _clear_oauth_query()
        st.rerun()
        return

    try:
        if connection is not None:
            WorkerClient(connection).complete_microsoft_oauth(code=code, state=state)
        else:
            graph_manager.complete(code=code, state=state)
    except (WorkerApiError, ValueError) as error:
        st.session_state["microsoft_oauth_error"] = str(error)
    except Exception as error:
        st.session_state["microsoft_oauth_error"] = (
            "Microsoftメール接続を完了できませんでした。\n\n"
            f"`{type(error).__name__} "
            f"{getattr(error, 'code', '') or ''}: {error}`"[:400]
        )
    else:
        st.session_state["microsoft_oauth_notice"] = (
            "Microsoftメールを読み取り専用で接続しました。"
        )
        st.session_state.pop("microsoft_authorization_url", None)
    finally:
        code = ""
        state = ""
        _clear_oauth_query()
    st.rerun()


def _query_value(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
    except (AttributeError, TypeError):
        return ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _clear_oauth_query() -> None:
    for name in ("code", "state", "error", "error_description", "session_state"):
        try:
            del st.query_params[name]
        except (KeyError, TypeError, AttributeError):
            pass


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
    month_filter = st.selectbox(
        "取引月",
        ("", *archive_months(all_receipts)),
        format_func=lambda value: (
            "すべての取引月" if not value else month_label(value).removesuffix("分")
        ),
        key="archive_month",
    )
    visible_receipts = filter_receipts(
        all_receipts,
        query=query,
        month=month_filter,
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
require_owner_access(st, st.secrets)
storage, drive_files, drive_error = load_drive_snapshot()
handle_microsoft_oauth_callback(storage)
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
