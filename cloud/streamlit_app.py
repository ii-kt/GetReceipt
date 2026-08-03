from __future__ import annotations

import logging
import os
import re
import shutil
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from src.automation.browser_session import ManagedBrowser, find_browser_executable
from src.automation import security_challenge as challenge_runtime
from src.automation.auth_challenges import profile_for
from src.automation.credentials import credentials_configured, service_credentials
from src.automation.mail_codes import (
    MailCodeUnavailableError,
    MailVerificationCodeReader,
    VERIFICATION_CODE_SOURCES,
)
from src.automation.providers import build_receipt_fetcher
from src.config import (
    DATA_DIR,
    RECEIPT_DRIVE_FOLDER_ID,
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
from src.storage.browser_profile_store import BrowserProfileStore
from src.storage.drive_storage import DriveStorage
from src.ui import remote_jobs
# Kept importable, and covered by its tests, so the in-app gate can be put
# back in one line if this app ever stops being privately shared.
from src.ui.access_control import require_owner_access  # noqa: F401
from src.ui import google_link
from src.ui.graph_link import (
    graph_manager_from_secrets as _build_graph_manager,
    render_graph_connection,
)
from src.ui import live_view
from src.ui.manual_upload import render_manual_upload
from src.ui.select_toggle import render_select_arrow_toggle
from src.ui import styles as ui_styles
from src.ui.module_contract import ensure_ui_module
from src.workflows.auto_acquisition import NOT_ISSUED_CODES, run_auto_acquisition
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
# Wi-Fi and electricity bill the month after use, so asking for the current
# month before the provider has issued it is expected, not a fault. The
# workflow decides what counts; the card only has to render it.
NOT_ISSUED_FAILURE_CODES = NOT_ISSUED_CODES
SECURITY_CHALLENGE_KEY = "getreceipt_security_challenge"
# Gates the owner has to work by hand on the provider's own page, because no
# code can express them. The browser is held open and mirrored instead.
INTERACTIVE_CHALLENGE_KINDS = frozenset({"captcha", "interactive"})
PUZZLE_OPEN_KEY = "getreceipt_puzzle_open"
SIGNIN_ATTEMPTS_KEY = "getreceipt_signin_attempts"
STATUS_STORAGE_KEY = "getreceipt_status_storage"
STORED_OUTCOMES_KEY = "getreceipt_stored_outcomes"
GRAPH_MANAGER_KEY = "getreceipt_graph_manager"
# One attempt at one service gets this long, end to end. Every wait inside is
# bounded on its own, but they add up: sign-in, then a step loop, then a
# download, then all of it again after a verification code - tens of minutes
# in the worst case, which on a phone is an acquisition that never ends. The
# budget is what makes that impossible rather than unlikely.
SERVICE_ATTEMPT_BUDGET_SECONDS = 150
# Continuing after the owner has supplied a code does the whole statement
# fetch, so it gets its own budget rather than the remains of the first.
SECURITY_RESUME_BUDGET_SECONDS = 210
# How long to look for a code in the mailbox before asking the owner instead.
# A provider that mails codes here has already answered within one poll; the
# rest is for one still in flight.
MAIL_CODE_WAIT_SECONDS = 45
# Keep this much of the attempt back for the sign-in that the code unlocks.
# Spending the whole budget waiting for the code leaves nothing to use it with.
MAIL_CODE_RESERVE_SECONDS = 60
# Every browser sign-in makes the provider mail or text a fresh verification
# code. Two is enough to survive one bad attempt; beyond that the owner is
# just being spammed, and repeated sign-ins are what risks an account lock.
MAX_SIGNIN_ATTEMPTS = 2
# ...but the cap has to let go on its own. Counted for good, it turned one bad
# afternoon into a month that could never be retried, while telling the owner
# to wait - which did nothing at all.
SIGNIN_COOL_OFF_SECONDS = 15 * 60
# Google lists only a handful of reasons a refresh token stops working, and
# the app cannot tell which one applied. Naming the one that is actionable -
# reissuing - beats sending the owner to change a setting that may already be
# correct. The seven-day expiry only ever applies while the consent screen is
# in Testing, so it is mentioned as a condition rather than as an instruction.
DRIVE_CREDENTIAL_EXPIRED = (
    "Googleの認証が失効しました。下から取り直してください。"
    "（同意画面が「テスト」の間に発行した認証情報は7日で失効します。"
    "「本番環境」で取り直せば期限切れは起きません）"
)
SECURITY_WAITING_PHASE = "awaiting_security_code"
SECURITY_SUBMITTING_PHASE = "submitting_security_code"
LOGGER = logging.getLogger(__name__)
MONTHLY_VIEW = "毎月の4件"
ARCHIVE_VIEW = "単発領収書"
EXPECTED_UI_API_VERSION = 2
EXPECTED_GOOGLE_LINK_API_VERSION = 2
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
# The reconnection card is reached only when Drive is already down, so a stale
# copy of it would crash the one screen that exists to recover from that.
google_link = ensure_ui_module(
    google_link,
    expected_version=EXPECTED_GOOGLE_LINK_API_VERSION,
    required_callables=("render_google_reconnect",),
)


st.set_page_config(
    page_title="GetReceipt",
    page_icon=":material/receipt_long:",
    layout="centered",
    initial_sidebar_state="collapsed",
)


def _credential_store() -> Any | None:
    from src.storage.google_credential_store import GoogleCredentialStore

    try:
        encryption_key = str(st.secrets["microsoft_graph"]["encryption_key"] or "")
    except Exception:
        return None
    if not encryption_key:
        return None
    try:
        return GoogleCredentialStore(
            folder_id=RECEIPT_DRIVE_FOLDER_ID, encryption_key=encryption_key
        )
    except Exception:
        return None


def recover_drive_from_store() -> DriveStorage | None:
    """Reconnect Drive with the credential kept from the last reconnection.

    The service account key in secrets never expires, and the stored file is
    shared with it, so this works precisely when the owner's own secret has
    stopped working.
    """

    from src.storage.drive_storage import (
        build_drive_service,
        build_user_drive_service,
        load_service_account_info,
        load_user_oauth_config,
    )

    store = _credential_store()
    oauth_config = load_user_oauth_config(st.secrets)
    if store is None or oauth_config is None:
        return None
    try:
        reader = build_drive_service(load_service_account_info(st.secrets))
        refresh_token = store.load(reader)
    except Exception:
        LOGGER.info("Stored Drive credential was unavailable")
        return None
    if not refresh_token or refresh_token == oauth_config.get("refresh_token"):
        return None
    try:
        service = build_user_drive_service({**oauth_config, "refresh_token": refresh_token})
        storage = DriveStorage(service, folder_id=RECEIPT_DRIVE_FOLDER_ID)
        storage.list_files()
    except Exception:
        LOGGER.info("Stored Drive credential no longer works")
        return None
    LOGGER.info("Drive reconnected from the stored credential")
    return storage


def remember_drive_credential(refresh_token: str) -> bool:
    """Keep a freshly issued credential so the next start needs no secrets edit."""

    from src.storage.drive_storage import (
        build_user_drive_service,
        load_service_account_info,
        load_user_oauth_config,
    )

    store = _credential_store()
    oauth_config = load_user_oauth_config(st.secrets)
    if store is None or oauth_config is None:
        return False
    try:
        service = build_user_drive_service({**oauth_config, "refresh_token": refresh_token})
        share_with = str(load_service_account_info(st.secrets).get("client_email") or "")
        return store.save(service, refresh_token, share_with=share_with)
    except Exception:
        LOGGER.info("Freshly issued Drive credential was not stored")
        return False


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
        detail = f"{type(error).__name__}: {error}".lower()
        if "invalid_grant" in detail or "token has been expired" in detail:
            # Secrets can only be edited by hand. A credential stored on the
            # last reconnection is read back here instead, so a stale secret
            # never has to be touched again.
            recovered = recover_drive_from_store()
            if recovered is not None:
                return recovered, recovered.list_files(), ""
        # A refresh token issued by an unpublished OAuth consent screen expires
        # after seven days. Without naming that, the failure looks like an
        # unrelated Drive outage.
        if "invalid_grant" in detail or "token has been expired" in detail:
            return None, [], DRIVE_CREDENTIAL_EXPIRED
        return None, [], "Google Driveの領収書フォルダを確認できませんでした。"


def _drive_credential_expired(drive_error: str) -> bool:
    return str(drive_error or "") == DRIVE_CREDENTIAL_EXPIRED


def graph_manager_from_secrets(secrets: Any, storage: Any) -> Any | None:
    """One Microsoft manager per visit, so one access token serves them all.

    Each manager mints its own token, and minting one costs a Drive read, a
    decryption and a round trip to Microsoft. Building a fresh manager for the
    mail-code reader, then the invoice reader, then the notice filer made a
    single acquisition pay that several times over while the owner watched.
    """

    cached = st.session_state.get(GRAPH_MANAGER_KEY)
    if isinstance(cached, tuple) and len(cached) == 2:
        cached_storage, manager = cached
        # A reconnected Drive is a different service object, and the manager
        # reads its token through that service. Only reuse one built on the
        # storage still in use.
        if cached_storage is storage and manager is not None:
            return manager
    manager = _build_graph_manager(secrets, storage)
    if manager is not None:
        st.session_state[GRAPH_MANAGER_KEY] = (storage, manager)
    return manager


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
    month_failures = dict(
        persisted_failures.get(target_month, {})
        if isinstance(persisted_failures, dict)
        else {}
    )
    # Session state is gone the moment the page reloads, so a status the owner
    # saw would be forgotten by the time they came back to look at it. The
    # copy kept beside the receipts is what makes it outlive the visit.
    for service_id, entry in stored_month_outcomes(target_month).items():
        month_failures.setdefault(service_id, entry)

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
            reason = str(remote_failure.get("detail") or "")
            code = str(remote_failure.get("code") or "")
            base = remote_failure.get("message", "自動取得に失敗しました。")
            if code in NOT_ISSUED_FAILURE_CODES:
                # The provider has simply not billed this month yet. Showing it
                # in red sends the owner looking for a fault that is not there,
                # so drop the error code and keep the plain explanation.
                status = "not_issued"
                detail = f"{base} {reason}".strip()
            else:
                status = "failed"
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
    # An abandoned attempt keeps the single browser slot for its whole TTL. The
    # owner starting a new acquisition supersedes it, so reclaim the slot;
    # otherwise the provider logs in and mails a code, and only then does the
    # lease creation fail.
    try:
        challenge_runtime.browser_lease_registry.close_all()
    except Exception as error:
        LOGGER.error(
            "Could not release a stale acquisition slot (%s)",
            type(error).__name__,
        )
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


def profile_store_from_secrets(storage: DriveStorage | None) -> Any | None:
    """Build the store that keeps each provider's browser recognisable."""

    if storage is None:
        return None
    try:
        encryption_key = str(st.secrets["microsoft_graph"]["encryption_key"] or "")
    except Exception:
        return None
    if not encryption_key:
        return None
    try:
        return BrowserProfileStore(
            drive_service=storage.service,
            folder_id=storage.folder_id,
            encryption_key=encryption_key,
        )
    except Exception:
        return None


def cleanup_runtime(
    browser: ManagedBrowser | None,
    run_dir: Path,
    *,
    profile_store: Any | None = None,
    service_id: str = "",
) -> str:
    errors: list[str] = []
    if browser is not None:
        try:
            # Close first: Chrome holds its cookie database open, so a copy
            # taken while it is running can be torn.
            browser.close(clear_profile=False)
        except Exception:
            errors.append("browser")
        if profile_store is not None and service_id:
            try:
                profile_store.save(service_id, run_dir / "profile")
            except Exception:
                # Being recognised next time is a convenience. It must never
                # cost the acquisition that just ran.
                LOGGER.info("Browser profile was not kept for %s", service_id)

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
    remember_month_outcome(
        target_month=target_month,
        service_id=service_id,
        code=code,
        message=message,
        detail=detail,
    )
    if code in NOT_ISSUED_FAILURE_CODES:
        # The provider answered perfectly well; there is simply no bill yet.
        # Counting that as a failed sign-in locked the service out of the
        # session for a month where nothing was wrong.
        clear_signin_attempts(service_id, target_month)
    return _advance_batch(batch)


def complete_batch_service(batch: dict[str, Any], service_id: str) -> bool:
    completed = list(batch.get("completed", ()))
    if service_id not in completed:
        completed.append(service_id)
    batch["completed"] = completed
    target_month = str(batch.get("target_month") or "")
    # The month is saved now, so any remembered reason is stale, and the
    # sign-ins it took are spent - a later run must not inherit that count and
    # refuse to start.
    forget_month_outcome(target_month=target_month, service_id=service_id)
    clear_signin_attempts(service_id, target_month)
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


def resume_with_mailed_code(
    *,
    service_id: str,
    target_month: str,
    fetcher: Any,
    storage: DriveStorage,
    result: Any,
    storage_secrets: Any,
    requested_after: datetime,
    status_box: Any,
    seconds_left: float | None = None,
) -> Any | None:
    """Finish a verification-code challenge using the owner's own mailbox.

    Returns the retried acquisition result, or None when the code could not be
    read and the owner must enter it manually.
    """

    challenge = getattr(result, "challenge", None)
    if str(getattr(challenge, "kind", "")) != "verification_code":
        return None
    source = VERIFICATION_CODE_SOURCES.get(service_id)
    resume = getattr(fetcher, "resume_after_security_code", None)
    if source is None or not callable(resume):
        return None
    graph_manager = graph_manager_from_secrets(storage_secrets, storage)
    if graph_manager is None:
        return None
    try:
        if not bool(graph_manager.status().get("connected")):
            return None
    except Exception:
        return None

    # Reading the mail is worth doing only if what it buys - the rest of the
    # acquisition - still fits in the attempt. Otherwise it is a wait that
    # ends in asking the owner for the code anyway, just later.
    mail_budget = MAIL_CODE_WAIT_SECONDS
    if seconds_left is not None:
        mail_budget = min(mail_budget, max(0.0, float(seconds_left) - MAIL_CODE_RESERVE_SECONDS))
        if mail_budget <= 0:
            return None

    status_box.write("メールに届いた確認コードを自動で読み取っています。")
    code = ""
    try:
        reader = MailVerificationCodeReader(graph_manager.access_token)
        code = reader.wait_for_code(
            source,
            requested_after=requested_after,
            timeout_seconds=mail_budget,
        )
        return run_auto_acquisition(
            service_id=service_id,
            target_month=target_month,
            fetcher=fetcher,
            storage=storage,
            on_progress=lambda event: status_box.write(event.message),
            fetch_statement=lambda month, value=code: resume(month, value),
        )
    except MailCodeUnavailableError:
        status_box.write("確認コードを自動取得できませんでした。手動入力へ切り替えます。")
        return None
    except Exception as error:
        LOGGER.warning(
            "Automatic verification code resume failed (%s)",
            type(error).__name__,
        )
        return None
    finally:
        code = ""


def file_billing_notice(
    service_id: str,
    *,
    target_month: str,
    graph_manager: Any = None,
    message_id: str = "",
    status_box: Any = None,
) -> None:
    """Mark this month's "your bill is ready" mail read and archive it.

    Only ever called once the month's PDF is confirmed in Drive, so the notice
    has already done its job. Best effort throughout: a mailbox that cannot be
    reached, or a notice that cannot be tied to this month, simply leaves the
    inbox as it was.
    """

    from src.automation.billing_notices import BILLING_NOTICE_SOURCES, BillingNoticeFiler

    if not message_id and service_id not in BILLING_NOTICE_SOURCES:
        return
    try:
        manager = graph_manager
        if manager is None:
            # Reuse the connection that actually worked. Rebuilding it from
            # secrets would fail whenever the stored credential is what is
            # holding Drive up, and the notice would never be filed again.
            storage = st.session_state.get(STATUS_STORAGE_KEY)
            if storage is None:
                return
            manager = graph_manager_from_secrets(st.secrets, storage)
        if manager is None or not bool(manager.status().get("connected")):
            return
        result = BillingNoticeFiler(manager.access_token).file_for_month(
            service_id,
            transaction_month=expected_transaction_month(service_id, target_month),
            message_id=message_id,
        )
    except Exception:
        LOGGER.info("Billing notice filing skipped for %s", service_id)
        return
    if result.filed and status_box is not None:
        try:
            status_box.write("請求のお知らせメールを既読にしてアーカイブしました。")
        except Exception:
            pass


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
        # The statement is this mail's own attachment, so the exact message is
        # already known and no month has to be guessed.
        file_billing_notice(
            "tokuten",
            target_month=target_month,
            graph_manager=graph_manager,
            message_id=getattr(fetcher, "source_message_id", ""),
            status_box=status_box,
        )

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
    # The limit is named up front so a slow provider reads as a run with an
    # end, not as a screen that has stopped responding.
    status_box = st.status(
        f"{position}/{len(service_ids)}  {service.label}を自動取得しています。"
        f"（最長{SERVICE_ATTEMPT_BUDGET_SECONDS // 60}分）",
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

    # The provider may already have answered this today, and a bill is issued
    # once a month: nothing it said this morning can have changed by tonight.
    # Signing in to be told the same thing again costs another verification
    # code, and for a month that cannot exist yet that code buys nothing.
    settled = settled_unissued_outcome(service_id, target_month)
    if settled is not None:
        batch_complete = fail_batch_service(
            batch,
            service_id,
            code=str(settled.get("code") or ""),
            message=str(settled.get("message") or ""),
            detail=str(settled.get("detail") or ""),
        )
        status_box.update(
            label=f"{service.label}はこの月の請求がまだ発行されていません。",
            state="complete",
        )
        st.rerun()

    # Signing in again means another verification code to the owner's phone or
    # mailbox. Stop before sending a third one and say so, rather than looking
    # like the app is stuck in a loop.
    if signin_attempts(service_id, target_month) >= MAX_SIGNIN_ATTEMPTS:
        minutes_left = signin_cool_off_remaining(service_id, target_month)
        batch_complete = fail_batch_service(
            batch,
            service_id,
            code="SIGNIN_ATTEMPT_LIMIT",
            message=(
                f"{service.label}のログインを{MAX_SIGNIN_ATTEMPTS}回試したため、"
                f"あと約{minutes_left}分お休みします。"
            ),
            detail=(
                "これ以上続けると確認コードが何度も届き、アカウントのロックにも"
                f"つながります。約{minutes_left}分後にもう一度実行すれば再開します。"
            ),
        )
        status_box.update(
            label=f"{service.label}のログインを中止しました。",
            state="error",
        )
        st.rerun()
    record_signin_attempt(service_id, target_month)

    run_dir = challenge_runtime.new_attempt_run_dir(service_id, target_month)
    browser: ManagedBrowser | None = None
    result = None
    unexpected_error = ""
    runtime_preserved = False
    profile_store = profile_store_from_secrets(storage)
    try:
        credentials = service_credentials(st.secrets, service_id)
        if profile_store is not None:
            # Start from the browser this provider already knows. A first-time
            # browser is what makes them ask for a puzzle or a fresh code.
            try:
                profile_store.restore(service_id, run_dir / "profile")
            except Exception:
                LOGGER.info("Stored browser profile was not used for %s", service_id)
        browser = ManagedBrowser(
            profile_dir=run_dir / "profile",
            download_dir=run_dir / "downloads",
        )
        # Everything this attempt does has to go through the browser, so the
        # budget lives there: no wait loop, here or in any fetcher, can outlive
        # it, and none of them has to remember to check.
        browser.set_deadline(SERVICE_ATTEMPT_BUDGET_SECONDS)
        attempt_deadline = time.monotonic() + SERVICE_ATTEMPT_BUDGET_SECONDS
        fetcher = build_receipt_fetcher(service_id, browser, credentials)
        attempt_started_at = datetime.now(timezone.utc)
        # The workflow is deliberately not given a cancellation check: once the
        # statement is in hand, a spent budget must not be the reason it is
        # thrown away instead of saved. Saving to Drive is bounded on its own.
        result = run_auto_acquisition(
            service_id=service_id,
            target_month=target_month,
            fetcher=fetcher,
            storage=storage,
            on_progress=lambda event: status_box.write(event.message),
        )
        if getattr(result, "action_required", False):
            # The provider mails the code to a mailbox this app can already
            # read, so finish the challenge without asking the owner.
            resumed = resume_with_mailed_code(
                service_id=service_id,
                target_month=target_month,
                fetcher=fetcher,
                storage=storage,
                result=result,
                storage_secrets=st.secrets,
                requested_after=attempt_started_at,
                status_box=status_box,
                seconds_left=attempt_deadline - time.monotonic(),
            )
            if resumed is not None:
                result = resumed
        if getattr(result, "action_required", False):
            challenge = getattr(result, "challenge", None)
            challenge_kind = str(getattr(challenge, "kind", "verification_code"))
            resume_attribute = (
                "resume_after_interactive_challenge"
                if challenge_kind in INTERACTIVE_CHALLENGE_KINDS
                else "resume_after_security_code"
            )
            if not callable(getattr(fetcher, resume_attribute, None)):
                raise RuntimeError("この請求元は安全な再開に対応していません。")
            # From here the owner is the one taking the time - reading a code
            # off their phone, or moving a puzzle piece. The attempt budget is
            # about the app spinning unattended, not about how long they take,
            # so the held browser is released from it.
            browser.set_deadline(None)
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
                "kind": challenge_kind,
                "message": str(getattr(challenge, "message", "確認コードの入力が必要です。")),
                "allowed_hosts": tuple(profile.allowed_hosts),
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
    except Exception as error:
        unexpected_error = (
            "自動取得を開始できませんでした。時間をおいて再試行してください。 "
            f"[{type(error).__name__}: {error}]"
        )[:300]
        LOGGER.warning("Acquisition attempt failed (%s)", type(error).__name__)
    finally:
        if not runtime_preserved:
            cleanup_error = cleanup_runtime(
                browser,
                run_dir,
                profile_store=profile_store,
                service_id=service_id,
            )
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

    file_billing_notice(service_id, target_month=target_month, status_box=status_box)
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
    interactive = str(challenge.get("kind") or "") in INTERACTIVE_CHALLENGE_KINDS
    service = service_by_id(service_id)
    result = None
    unexpected_error = ""
    resume_detail = ""
    status_box = st.status(
        f"{service.label}の自動取得を再開しています。"
        if interactive
        else f"{service.label}へ確認コードを送信し、自動取得を再開しています。",
        expanded=True,
    )

    profile_store = profile_store_from_secrets(storage)
    solved_browser = None
    solved_profile_dir = None
    try:
        with challenge_runtime.browser_lease_registry.checkout(
            token,
            expected_service_id=service_id,
            expected_target_month=target_month,
        ) as lease:
            credentials = service_credentials(st.secrets, service_id)
            fetcher = build_receipt_fetcher(service_id, lease.browser, credentials)
            solved_browser = lease.browser
            solved_profile_dir = Path(lease.run_dir) / "profile"
            # The owner has done their part; from here it is the app running
            # unattended again, so it runs against a budget again.
            lease.browser.set_deadline(SECURITY_RESUME_BUDGET_SECONDS)
            if interactive:
                # The owner cleared the gate on the live page themselves. This
                # only picks the acquisition back up on that same tab.
                resume = getattr(fetcher, "resume_after_interactive_challenge", None)
                fetch_statement = lambda month: resume(month)  # noqa: E731
            else:
                resume = getattr(fetcher, "resume_after_security_code", None)
                fetch_statement = lambda month: resume(month, code)  # noqa: E731
            if not callable(resume):
                raise RuntimeError("この請求元は安全な再開に対応していません。")
            result = run_auto_acquisition(
                service_id=service_id,
                target_month=target_month,
                fetcher=fetcher,
                storage=storage,
                on_progress=lambda event: status_box.write(event.message),
                fetch_statement=fetch_statement,
            )
    except challenge_runtime.BrowserLeaseUnavailableError:
        # Signing in again would mail another code. Leave the decision to the
        # owner instead of doing it for them.
        status_box.update(label=f"{service.label}の待機時間が切れました。", state="error")
        st.session_state[SECURITY_CHALLENGE_KEY] = {**challenge, "token": ""}
        st.rerun()
    except Exception as error:
        unexpected_error = (
            "確認コード送信後の自動取得を再開できませんでした。"
            "新しい確認コードで再試行してください。"
        )
        resume_detail = f"{type(error).__name__}: {error}"[:300]
        LOGGER.warning("Security code resume failed (%s)", resume_detail)

    if result is not None and getattr(result, "action_required", False):
        # The gate is still up, so the browser goes back to the owner and the
        # budget comes back off it.
        if solved_browser is not None:
            solved_browser.set_deadline(None)
        updated = dict(challenge)
        if interactive:
            # The gate is still up: either the piece is not in place yet or the
            # provider issued a fresh puzzle. Keep the same browser and let the
            # owner try again rather than starting the sign-in over.
            challenge_message = str(
                getattr(getattr(result, "challenge", None), "message", "")
            )
            updated["error"] = (
                challenge_message
                or "パズルがまだ完成していません。ピースを枠に合わせてください。"
            )
            st.session_state[SECURITY_CHALLENGE_KEY] = updated
            st.session_state[f"{PUZZLE_OPEN_KEY}_{token}"] = True
            status_box.update(label="パズルをもう一度確認してください。", state="error")
            st.rerun()
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

    # Whatever the provider granted for clearing this challenge lives in the
    # cookies of that browser. Keep them, so the next month is not challenged
    # from scratch again.
    if (
        profile_store is not None
        and solved_browser is not None
        and solved_profile_dir is not None
        and result is not None
        and getattr(result, "success", False)
    ):
        try:
            solved_browser.close(clear_profile=False)
            profile_store.save(service_id, solved_profile_dir)
        except Exception:
            LOGGER.info("Browser profile was not kept for %s", service_id)

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

    file_billing_notice(service_id, target_month=target_month, status_box=status_box)
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


def status_store() -> Any | None:
    """The record of month outcomes kept beside the receipts in Drive."""

    from src.storage.status_store import ServiceStatusStore

    storage = st.session_state.get(STATUS_STORAGE_KEY)
    if storage is None:
        return None
    try:
        return ServiceStatusStore(storage.service, RECEIPT_DRIVE_FOLDER_ID)
    except Exception:
        return None


def stored_month_outcomes(target_month: str) -> dict[str, dict[str, str]]:
    cached = st.session_state.get(STORED_OUTCOMES_KEY)
    if not isinstance(cached, dict):
        store = status_store()
        cached = store.load() if store is not None else {}
        st.session_state[STORED_OUTCOMES_KEY] = cached
    entries = cached.get(target_month)
    return dict(entries) if isinstance(entries, dict) else {}


def remember_month_outcome(
    *, target_month: str, service_id: str, code: str, message: str, detail: str
) -> None:
    store = status_store()
    if store is None:
        return
    try:
        store.record(
            target_month=target_month,
            service_id=service_id,
            code=code,
            message=message,
            detail=detail,
        )
    except Exception:
        LOGGER.info("Month outcome was not stored for %s", service_id)
    st.session_state.pop(STORED_OUTCOMES_KEY, None)


def forget_month_outcome(*, target_month: str, service_id: str) -> None:
    store = status_store()
    if store is None:
        return
    try:
        store.clear(target_month=target_month, service_id=service_id)
    except Exception:
        LOGGER.info("Month outcome was not cleared for %s", service_id)
    st.session_state.pop(STORED_OUTCOMES_KEY, None)


def settled_unissued_outcome(
    service_id: str, target_month: str
) -> dict[str, str] | None:
    """The provider's own "not billed yet" answer, if it gave one today.

    A statement is issued once a month on the provider's own schedule, so an
    answer from earlier the same day cannot have changed. Returning it saves a
    sign-in - and with it a verification code to the owner's phone - for a
    month that is simply not there yet. It is deliberately only good for the
    calendar day: tomorrow the question is worth asking again.
    """

    entry = stored_month_outcomes(target_month).get(service_id)
    if not isinstance(entry, dict):
        return None
    if str(entry.get("code") or "") not in NOT_ISSUED_CODES:
        return None
    try:
        answered_at = datetime.fromisoformat(str(entry.get("at") or ""))
    except ValueError:
        return None
    if answered_at.tzinfo is None:
        answered_at = answered_at.replace(tzinfo=timezone.utc)
    if answered_at.astimezone(TOKYO).date() != datetime.now(TOKYO).date():
        return None
    return dict(entry)


def _recent_signin_times(service_id: str, target_month: str) -> list[float]:
    """The sign-ins for this service-month still inside the cool-off window.

    The cap used to be a plain count that nothing ever reset, so the advice it
    printed - wait a while and run it again - was untrue: waiting changed
    nothing, and the month stayed refused for as long as the tab stayed open.
    Counting only recent sign-ins makes the wait do what it says.
    """

    recorded = st.session_state.get(SIGNIN_ATTEMPTS_KEY, {})
    if not isinstance(recorded, dict):
        return []
    stamps = recorded.get(f"{service_id}:{target_month}")
    if not isinstance(stamps, (list, tuple)):
        return []
    cutoff = time.monotonic() - SIGNIN_COOL_OFF_SECONDS
    return sorted(
        float(stamp)
        for stamp in stamps
        if isinstance(stamp, (int, float)) and float(stamp) > cutoff
    )


def signin_attempts(service_id: str, target_month: str) -> int:
    return len(_recent_signin_times(service_id, target_month))


def signin_cool_off_remaining(service_id: str, target_month: str) -> int:
    """Whole minutes until this service may sign in again, at least one."""

    stamps = _recent_signin_times(service_id, target_month)
    if not stamps:
        return 0
    seconds = SIGNIN_COOL_OFF_SECONDS - (time.monotonic() - stamps[0])
    return max(1, int(seconds // 60) + (1 if seconds % 60 else 0))


def record_signin_attempt(service_id: str, target_month: str) -> int:
    """Count one browser sign-in, which is one verification code to the owner."""

    recorded = dict(st.session_state.get(SIGNIN_ATTEMPTS_KEY, {}) or {})
    key = f"{service_id}:{target_month}"
    stamps = _recent_signin_times(service_id, target_month)
    stamps.append(time.monotonic())
    recorded[key] = stamps
    st.session_state[SIGNIN_ATTEMPTS_KEY] = recorded
    return len(stamps)


def clear_signin_attempts(service_id: str, target_month: str) -> None:
    recorded = dict(st.session_state.get(SIGNIN_ATTEMPTS_KEY, {}) or {})
    recorded.pop(f"{service_id}:{target_month}", None)
    st.session_state[SIGNIN_ATTEMPTS_KEY] = recorded


def offer_expired_challenge_restart(
    batch: dict[str, Any],
    challenge: dict[str, Any],
    service: Any,
) -> None:
    """Say the wait ran out, and let the owner decide about another code."""

    st.warning(
        f"{service.label}の待機時間が切れました。"
        "やり直すと確認コードがもう一通届きます。",
        icon=":material/timer_off:",
    )
    columns = st.columns(2)
    if columns[0].button(
        "やり直す（コードが再送されます）",
        use_container_width=True,
        icon=":material/refresh:",
    ):
        restart_security_challenge(batch, challenge)
    if columns[1].button("中止する", use_container_width=True, type="primary"):
        token = str(challenge.get("token") or "")
        if token:
            challenge_runtime.browser_lease_registry.discard(token)
        st.session_state.pop(SECURITY_CHALLENGE_KEY, None)
        fail_batch_service(
            batch,
            str(challenge.get("service_id") or ""),
            code="SECURITY_CODE_TIMEOUT",
            message=f"{service.label}の確認コード待機を中止しました。",
            detail="時間をおいて、もう一度実行してください。",
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
        # Restarting means signing in again, which mails another code. That is
        # the owner's call, not something to do silently while they are away.
        offer_expired_challenge_restart(batch, challenge, service)
        return

    if metadata.service_id != service_id or metadata.target_month != target_month:
        restart_security_challenge(batch, challenge)

    expires_label = metadata.expires_at.astimezone(TOKYO).strftime("%H:%M")
    message = str(challenge.get("message") or "追加の本人確認コードが必要です。")
    interactive = str(challenge.get("kind") or "") in INTERACTIVE_CHALLENGE_KINDS
    if interactive:
        st.warning(
            f"{service.label}: {message}"
            " 下の画面で操作してください。自動取得は終了せず、同じChromeで待機しています。",
            icon=":material/touch_app:",
        )
    else:
        st.warning(
            f"{service.label}: {message}"
            " iPhoneで確認し、この画面へ戻って入力してください。自動取得は終了せず待機しています。",
            icon=":material/mark_email_unread:",
        )
    st.caption(f"操作期限の目安: {expires_label}　このタブは閉じたり再読み込みしたりしないでください。")
    error_message = str(challenge.get("error") or "")
    if error_message:
        st.error(error_message, icon=":material/error:")

    if interactive:
        render_interactive_challenge(storage, batch, challenge, token)
        return

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


def interactive_challenge_fingerprint(challenge: dict[str, Any]) -> str:
    """Digest of the puzzle's answer field, for telling moved from untouched."""

    token = str(challenge.get("token") or "")
    service_id = str(challenge.get("service_id") or "")
    if not token or not service_id:
        return ""
    try:
        with challenge_runtime.browser_lease_registry.checkout(
            token,
            expected_service_id=service_id,
            expected_target_month=str(challenge.get("target_month") or ""),
        ) as lease:
            fetcher = build_receipt_fetcher(
                service_id, lease.browser, service_credentials(st.secrets, service_id)
            )
            reader = getattr(fetcher, "interactive_challenge_state", None)
            if not callable(reader):
                return ""
            return str((reader() or {}).get("fingerprint") or "")
    except Exception:
        return ""


def render_interactive_challenge(
    storage: DriveStorage,
    batch: dict[str, Any],
    challenge: dict[str, Any],
    token: str,
) -> None:
    """Mirror the provider's page so the owner can clear its gate by hand.

    Epos guards its sign-in with a slide puzzle, which no code can express and
    which the app must not answer for the owner. Holding the same Chrome open
    and showing it here keeps every other step automatic: once the owner has
    worked the control, the acquisition carries on from that very page.
    """

    opened_key = f"{PUZZLE_OPEN_KEY}_{token}"
    if not st.session_state.get(opened_key):
        st.caption(
            "パズルを開いて、ピースを枠に合わせてください。"
            "合わせ終わったら「🧩 解除して自動取得を続ける」を押します。"
        )
        if st.button(
            "🧩 パズルを開く",
            type="primary",
            use_container_width=True,
        ):
            st.session_state[opened_key] = True
            # Remember the answer as it arrives. The page ships that field
            # already holding the piece's starting position, so only a change
            # in it shows the owner has actually moved anything.
            st.session_state[f"{opened_key}__before"] = interactive_challenge_fingerprint(
                challenge
            )
            st.rerun()
        return

    try:
        with challenge_runtime.browser_lease_registry.checkout(
            token,
            expected_service_id=str(challenge.get("service_id") or ""),
            expected_target_month=str(challenge.get("target_month") or ""),
        ) as lease:
            live_view.render_live_view(
                st,
                lease.browser,
                key=f"live_view_{token}",
                allowed_hosts=tuple(challenge.get("allowed_hosts") or ()),
            )
    except challenge_runtime.BrowserLeaseUnavailableError:
        st.session_state.pop(opened_key, None)
        restart_security_challenge(batch, challenge)

    if st.button(
        "🧩 解除して自動取得を続ける",
        type="primary",
        use_container_width=True,
    ):
        before = str(st.session_state.get(f"{opened_key}__before") or "")
        now = interactive_challenge_fingerprint(challenge)
        if before and now and before == now:
            # Sending an untouched puzzle only earns the provider's "wrong
            # puzzle" page, and that costs the whole sign-in.
            st.error(
                "ピースがまだ動いていません。画面でピースを枠に合わせてから、"
                "もう一度押してください。",
                icon=":material/drag_pan:",
            )
            st.session_state[opened_key] = True
            return
        st.session_state.pop(opened_key, None)
        st.session_state.pop(f"{opened_key}__before", None)
        resume_security_code(storage, batch, challenge, "")

    if st.button(
        "最初からやり直す",
        use_container_width=True,
        icon=":material/refresh:",
    ):
        st.session_state.pop(opened_key, None)
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
        # The credential can only be replaced by hand, but it must at least be
        # replaceable from the phone rather than from a desktop script.
        if _drive_credential_expired(drive_error):
            google_link.render_google_reconnect(
                st, st.secrets, remember=remember_drive_credential
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
    # The mail connection is needed both for the electricity invoice and to
    # read a provider's login code. Offering it only for electricity left no
    # way to connect when that was already saved and a code was still needed.
    mail_dependent_missing = any(
        service.id == "tokuten" or service.id in VERIFICATION_CODE_SOURCES
        for service in missing_services
    )
    graph_connected = False
    if graph_manager is not None and mail_dependent_missing and not active_batch:
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
        # The credential can only be replaced by hand, but it must at least be
        # replaceable from the phone rather than from a desktop script.
        if _drive_credential_expired(drive_error):
            google_link.render_google_reconnect(
                st, st.secrets, remember=remember_drive_credential
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
render_select_arrow_toggle(st)
# Access is now gated by Streamlit Cloud itself: this app is shared with one
# address and an anonymous visitor never reaches it, verified against the live
# URL. The in-app sign-in on top of that only cost the owner a second login.
#
# That sharing setting is therefore the ONLY thing standing between the public
# and the receipts, the stored provider credentials, and the buttons that sign
# in to those providers. Setting the app back to public exposes all of it. To
# put the second gate back, call require_owner_access(st, st.secrets) here -
# the module and its tests are kept intact for exactly that.
storage, drive_files, drive_error = load_drive_snapshot()
# The month outcomes are read from and written to the same folder, so the
# store needs whichever Drive connection actually worked this time.
st.session_state[STATUS_STORAGE_KEY] = storage
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
