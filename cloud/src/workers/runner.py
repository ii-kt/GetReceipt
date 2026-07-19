from __future__ import annotations

import hmac
import logging
import re
import shutil
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from ..automation.browser_session import ManagedBrowser
from ..automation.providers import build_receipt_fetcher
from ..jobs.inbox import ChallengeResponseInbox
from ..jobs.models import (
    BatchJob,
    BatchJobState,
    Challenge,
    ChallengeInputSchema,
    ChallengeInputType,
    ChallengeType,
    utc_now,
)
from ..jobs.store import SQLiteJobStore, VersionConflictError
from ..workflows.auto_acquisition import run_auto_acquisition


LOGGER = logging.getLogger(__name__)
_SAFE_SERVICE_ID = re.compile(r"^[a-z0-9_-]{1,64}$")
_INTERACTIVE_CHALLENGE_KINDS = {
    "captcha",
    "interactive",
    "consent",
    "push_approval",
    "passkey_unavailable",
}
_VIEWER_ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "epos": frozenset({"www.eposcard.co.jp"}),
    "commufa": frozenset({"mypage.commufa.jp"}),
    "mobile": frozenset(
        {
            "webbilling.ntt-finance.co.jp",
            "id.smt.docomo.ne.jp",
        }
    ),
    "webbilling": frozenset(
        {
            "webbilling.ntt-finance.co.jp",
            "id.smt.docomo.ne.jp",
        }
    ),
    "tokuten": frozenset(
        {
            "outlook.live.com",
            "login.live.com",
            "account.live.com",
            "login.microsoftonline.com",
        }
    ),
}
_NON_CONTINUABLE_FAILURE_CODES = {
    "INVALID_REQUEST",
    "SAVED_FILE_NOT_FOUND",
}
_NON_CONTINUABLE_FAILURE_PREFIXES = (
    "DRIVE_",
    "SECURITY_",
)
_NON_CONTINUABLE_FAILURE_MARKERS = (
    "LOCK",
    "BLOCK",
    "SUSPEND",
    "DISABLED",
    "ORIGIN_MISMATCH",
)

StorageFactory = Callable[[], Any]
CredentialsFactory = Callable[[str], Mapping[str, str]]
BrowserFactory = Callable[..., ManagedBrowser]
FetcherFactory = Callable[[str, ManagedBrowser, dict[str, str]], Any]
AcquisitionRunner = Callable[..., Any]


class ViewerUnavailableError(RuntimeError):
    pass


class ReceiptSaveCancelledError(RuntimeError):
    code = "ACQUISITION_CANCELLED"


class _CancellationAwareStorage:
    """Linearize a Drive write with durable owner cancellation."""

    def __init__(self, *, storage: Any, worker: "ReceiptWorker", job_id: Any) -> None:
        self._storage = storage
        self._worker = worker
        self._job_id = job_id

    def list_files(self) -> Any:
        return self._storage.list_files()

    def upsert_bytes(self, **kwargs: Any) -> Any:
        with self._worker.receipt_commit_guard():
            if self._worker._cancellation_requested(self._job_id):
                raise ReceiptSaveCancelledError(
                    "The job was cancelled before the Drive save began."
                )
            return self._storage.upsert_bytes(**kwargs)


@dataclass(frozen=True)
class WorkerRuntimeConfig:
    owner_id: str
    profile_root: Path
    download_root: Path
    challenge_ttl_seconds: float = 10 * 60
    poll_interval_seconds: float = 0.5

    def __post_init__(self) -> None:
        owner = str(self.owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id is required")
        if self.challenge_ttl_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise ValueError("worker timeouts must be positive")
        object.__setattr__(self, "owner_id", owner)
        object.__setattr__(self, "profile_root", Path(self.profile_root).resolve())
        object.__setattr__(self, "download_root", Path(self.download_root).resolve())


class ReceiptWorker:
    """Single-account persistent worker.

    The browser profile is stable per provider. Downloads are isolated per job
    and removed after a terminal provider attempt. Only one acquisition is
    claimed at a time, matching the personal-account lock semantics.
    """

    def __init__(
        self,
        *,
        store: SQLiteJobStore,
        inbox: ChallengeResponseInbox,
        config: WorkerRuntimeConfig,
        storage_factory: StorageFactory,
        credentials_factory: CredentialsFactory,
        browser_factory: BrowserFactory = ManagedBrowser,
        fetcher_factory: FetcherFactory = build_receipt_fetcher,
        acquisition_runner: AcquisitionRunner = run_auto_acquisition,
        fatal_callback: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.inbox = inbox
        self.config = config
        self.storage_factory = storage_factory
        self.credentials_factory = credentials_factory
        self.browser_factory = browser_factory
        self.fetcher_factory = fetcher_factory
        self.acquisition_runner = acquisition_runner
        self._fatal_callback = fatal_callback
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lifecycle_lock = RLock()
        self._receipt_commit_lock = RLock()
        self._active_job_id = ""
        self._viewer_lock = RLock()
        self._active_browser: ManagedBrowser | None = None
        self._active_browser_job_id = ""
        self._interactive_challenge_id = ""
        self._interactive_service_id = ""
        self._interactive_target_id = ""
        self._interactive_continue = Event()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop_event.is_set())

    @property
    def active_job_id(self) -> str:
        return self._active_job_id

    @contextmanager
    def receipt_commit_guard(self) -> Iterator[None]:
        """Order owner cancellation and Drive writes in this single worker."""

        with self._receipt_commit_lock:
            yield

    def capture_viewer_frame(self, job_id: str, challenge_id: str) -> bytes:
        with self._viewer_lock:
            browser = self._require_active_browser(job_id, challenge_id)
            frame = browser.screenshot_current_page()
        if not frame.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ViewerUnavailableError("取得ブラウザの画面を確認できません。")
        return frame

    def send_viewer_input(
        self,
        job_id: str,
        challenge_id: str,
        *,
        action: str,
        x: int | None = None,
        y: int | None = None,
        text: str = "",
        key: str = "",
    ) -> None:
        normalized_action = str(action or "").strip().lower()
        with self._viewer_lock:
            browser = self._require_active_browser(job_id, challenge_id)
            if normalized_action == "click":
                if x is None or y is None or not (0 <= x <= 4096 and 0 <= y <= 4096):
                    raise ValueError("クリック位置が不正です。")
                browser.click_current_page(int(x), int(y))
                return
            if normalized_action == "text":
                value = str(text or "")
                if not value or len(value) > 512:
                    raise ValueError("送信する文字列が不正です。")
                browser.insert_text_current_page(value)
                return
            if normalized_action == "key":
                normalized_key = str(key or "")
                if normalized_key not in {"Enter", "Tab", "Escape", "Backspace"}:
                    raise ValueError("キー操作が不正です。")
                browser.press_key_current_page(normalized_key)
                return
        raise ValueError("ブラウザ操作が不正です。")

    def continue_interactive(self, job_id: str, challenge_id: str) -> None:
        with self._viewer_lock:
            self._require_active_browser(job_id, challenge_id)
            self._interactive_continue.set()

    def notify_job_cancelled(self, job_id: str) -> None:
        with self._viewer_lock:
            if self._active_browser_job_id and hmac.compare_digest(
                self._active_browser_job_id.encode(),
                str(job_id or "").encode(),
            ):
                self._interactive_continue.set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            self.config.profile_root.mkdir(parents=True, exist_ok=True)
            self.config.download_root.mkdir(parents=True, exist_ok=True)
            self.store.recover_incomplete_jobs(owner=self.config.owner_id)
            self._stop_event.clear()
            self._thread = Thread(
                target=self._run_loop,
                name="getreceipt-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 45) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.1, timeout_seconds))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def run_once(self) -> bool:
        job = self.store.claim_next(owner=self.config.owner_id)
        if job is None:
            return False
        self._active_job_id = str(job.id)
        try:
            self._process_job(job)
        except VersionConflictError:
            # User cancellation or another accepted state transition won.
            LOGGER.info("Worker job state changed while processing %s", job.id)
        except Exception as error:
            LOGGER.error("Worker job failed with %s", type(error).__name__)
            self._fail_unexpected(job.id)
        finally:
            self._active_job_id = ""
        return True

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                processed = self.run_once()
                if not processed:
                    self._stop_event.wait(self.config.poll_interval_seconds)
        except Exception as error:
            LOGGER.critical(
                "Worker loop terminated unexpectedly (%s)",
                type(error).__name__,
            )
            callback = self._fatal_callback
            if callback is not None and not self._stop_event.is_set():
                try:
                    callback()
                except Exception as callback_error:
                    LOGGER.error(
                        "Worker fatal callback failed (%s)",
                        type(callback_error).__name__,
                    )

    def _process_job(self, claimed: BatchJob) -> None:
        storage = self.storage_factory()
        job = claimed
        while not self._stop_event.is_set():
            job = self.store.get_job(job.id, owner=self.config.owner_id)
            if job.state is BatchJobState.CANCELLED:
                return
            failed_service_ids, service_failures = _service_failure_result(job.result)
            remaining = [
                service_id
                for service_id in job.service_ids
                if service_id not in job.completed
                and service_id not in failed_service_ids
            ]
            if not remaining:
                has_failures = bool(failed_service_ids)
                self.store.compare_and_set(
                    job.id,
                    owner=self.config.owner_id,
                    expected_version=job.version,
                    state=(
                        BatchJobState.FAILED
                        if has_failures
                        else BatchJobState.SUCCEEDED
                    ),
                    current=None,
                    error=(
                        {
                            "code": "PARTIAL_ACQUISITION_FAILED",
                            "message": "一部サービスの自動取得を完了できませんでした。",
                            "retryable": True,
                        }
                        if has_failures
                        else None
                    ),
                    result=_batch_result(
                        completed=job.completed,
                        failed_service_ids=failed_service_ids,
                        service_failures=service_failures,
                    ),
                    event_type=(
                        "job_partially_failed"
                        if has_failures
                        else "job_succeeded"
                    ),
                    event_payload={
                        "completed_service_ids": list(job.completed),
                        "failed_service_ids": list(failed_service_ids),
                    },
                )
                return

            service_id = remaining[0]
            if job.state is not BatchJobState.RUNNING or job.current != service_id:
                job = self.store.compare_and_set(
                    job.id,
                    owner=self.config.owner_id,
                    expected_version=job.version,
                    state=BatchJobState.RUNNING,
                    current=service_id,
                    event_type="service_started",
                    event_payload={"service_id": service_id},
                )

            result = self._acquire_service(job, service_id, storage)
            if result is None:
                return
            if not bool(getattr(result, "success", False)):
                if self._finish_failed_result(job.id, service_id, result):
                    continue
                return

            current = self.store.get_job(job.id, owner=self.config.owner_id)
            if current.state in {
                BatchJobState.CANCELLED,
                BatchJobState.FAILED,
                BatchJobState.INTERVENTION_REQUIRED,
            }:
                return
            completed = tuple(dict.fromkeys((*current.completed, service_id)))
            failed_service_ids, service_failures = _service_failure_result(current.result)
            next_service = next(
                (
                    candidate
                    for candidate in current.service_ids
                    if candidate not in completed
                    and candidate not in failed_service_ids
                ),
                None,
            )
            terminal = next_service is None
            has_failures = bool(failed_service_ids)
            job = self.store.compare_and_set(
                current.id,
                owner=self.config.owner_id,
                expected_version=current.version,
                state=(
                    BatchJobState.FAILED
                    if terminal and has_failures
                    else (
                        BatchJobState.SUCCEEDED
                        if terminal
                        else BatchJobState.RUNNING
                    )
                ),
                completed=completed,
                current=next_service,
                error=(
                    {
                        "code": "PARTIAL_ACQUISITION_FAILED",
                        "message": "一部サービスの自動取得を完了できませんでした。",
                        "retryable": True,
                    }
                    if terminal and has_failures
                    else None
                ),
                result=_batch_result(
                    completed=completed,
                    failed_service_ids=failed_service_ids,
                    service_failures=service_failures,
                ),
                event_type="service_completed",
                event_payload={
                    "service_id": service_id,
                    "job_terminal": terminal,
                },
            )
            if terminal:
                return

    def _acquire_service(self, job: BatchJob, service_id: str, storage: Any) -> Any | None:
        safe_service = self._service_id(service_id)
        guarded_storage = _CancellationAwareStorage(
            storage=storage,
            worker=self,
            job_id=job.id,
        )
        attempt_id = uuid4().hex
        profile_dir = self.config.profile_root / safe_service
        attempt_dir = self.config.download_root / str(job.id) / f"{safe_service}-{attempt_id}"
        download_dir = attempt_dir / "downloads"
        browser: ManagedBrowser | None = None
        try:
            browser = self.browser_factory(
                profile_dir=profile_dir,
                download_dir=download_dir,
            )
            with self._viewer_lock:
                self._active_browser = browser
                self._active_browser_job_id = str(job.id)
            credentials = dict(self.credentials_factory(safe_service))
            fetcher = self.fetcher_factory(safe_service, browser, credentials)
            result = self.acquisition_runner(
                service_id=safe_service,
                target_month=job.target_month,
                fetcher=fetcher,
                storage=guarded_storage,
                cancellation_requested=lambda: self._cancellation_requested(job.id),
            )
            challenge_attempts = 0
            while bool(getattr(result, "action_required", False)):
                challenge_attempts += 1
                if challenge_attempts > 3:
                    self._set_intervention(
                        job.id,
                        code="CHALLENGE_ATTEMPTS_EXHAUSTED",
                        message="追加認証の試行回数上限に達したため、安全に停止しました。",
                    )
                    return None
                challenge_kind = str(
                    getattr(getattr(result, "challenge", None), "kind", "") or ""
                )
                if challenge_kind == "verification_code":
                    resume = getattr(fetcher, "resume_after_security_code", None)
                    if not callable(resume):
                        self._set_intervention(
                            job.id,
                            code="CHALLENGE_RESUME_UNSUPPORTED",
                            message="表示された追加認証を同じブラウザで安全に再開できません。",
                        )
                        return None
                    response = self._wait_for_code(job.id, safe_service, result)
                    if response is None:
                        return None
                    try:
                        result = self.acquisition_runner(
                            service_id=safe_service,
                            target_month=job.target_month,
                            fetcher=fetcher,
                            storage=guarded_storage,
                            fetch_statement=lambda month, value=response: resume(month, value),
                            cancellation_requested=lambda: self._cancellation_requested(job.id),
                        )
                    finally:
                        response = ""
                    continue
                if challenge_kind in _INTERACTIVE_CHALLENGE_KINDS:
                    if not self._wait_for_interactive(
                        job.id,
                        safe_service,
                        result,
                        challenge_kind=challenge_kind,
                    ):
                        return None
                    result = self.acquisition_runner(
                        service_id=safe_service,
                        target_month=job.target_month,
                        fetcher=fetcher,
                        storage=guarded_storage,
                        cancellation_requested=lambda: self._cancellation_requested(job.id),
                    )
                    continue
                self._set_intervention(
                    job.id,
                    code="CHALLENGE_KIND_UNSUPPORTED",
                    message="未確認の追加認証が表示されたため、安全に停止しました。",
                )
                return None
            return result
        finally:
            with self._viewer_lock:
                if self._active_browser is browser:
                    self._active_browser = None
                    self._active_browser_job_id = ""
                    self._interactive_challenge_id = ""
                    self._interactive_service_id = ""
                    self._interactive_target_id = ""
                    self._interactive_continue.clear()
            if browser is not None:
                try:
                    browser.close(clear_profile=False)
                except Exception as error:
                    LOGGER.error(
                        "Failed to close persistent worker browser (%s)",
                        type(error).__name__,
                    )
            self._remove_attempt_dir(attempt_dir)

    def _wait_for_code(self, job_id: Any, service_id: str, result: Any) -> str | None:
        current = self.store.get_job(job_id, owner=self.config.owner_id)
        challenge_type, schema = _challenge_contract(
            service_id,
            challenge_kind="verification_code",
        )
        challenge = Challenge.new(
            job_id=current.id,
            type=challenge_type,
            message=str(
                getattr(getattr(result, "challenge", None), "message", "")
                or "iPhoneで確認コードを入力してください。"
            ),
            input_schema=schema,
            metadata={"service_id": service_id},
            expires_at=utc_now() + timedelta(seconds=self.config.challenge_ttl_seconds),
        )
        self.store.add_challenge(challenge, owner=self.config.owner_id)
        self.inbox.register(
            challenge_id=str(challenge.id),
            owner_id=self.config.owner_id,
            ttl_seconds=self.config.challenge_ttl_seconds,
        )
        waiting = self.store.compare_and_set(
            current.id,
            owner=self.config.owner_id,
            expected_version=current.version,
            state=BatchJobState.WAITING_FOR_CHALLENGE,
            current=service_id,
            event_type="challenge_waiting",
            event_payload={
                "challenge_id": str(challenge.id),
                "challenge_type": challenge.type.value,
                "service_id": service_id,
            },
        )

        response: str | None = None
        while not self._stop_event.is_set() and self.inbox.is_pending(str(challenge.id)):
            response = self.inbox.wait_and_consume(
                str(challenge.id),
                timeout_seconds=min(1.0, self.config.challenge_ttl_seconds),
            )
            if response is not None:
                break
        if response is None:
            self.inbox.discard(str(challenge.id))
            if self._stop_event.is_set():
                # Process shutdown is not a user cancellation or expiry.
                # Leave the durable waiting state for startup recovery.
                return None
            latest = self.store.get_job(waiting.id, owner=self.config.owner_id)
            if latest.state is BatchJobState.CANCELLED:
                return None
            self.store.compare_and_set(
                latest.id,
                owner=self.config.owner_id,
                expected_version=latest.version,
                state=BatchJobState.FAILED,
                error={
                    "code": "CHALLENGE_EXPIRED",
                    "message": "追加認証の入力期限が切れたため、この試行を終了しました。",
                    "retryable": True,
                },
                event_type="challenge_expired",
                event_payload={"challenge_id": str(challenge.id)},
            )
            return None

        latest = self.store.get_job(waiting.id, owner=self.config.owner_id)
        self.store.compare_and_set(
            latest.id,
            owner=self.config.owner_id,
            expected_version=latest.version,
            state=BatchJobState.RUNNING,
            current=service_id,
            event_type="challenge_response_consumed",
            event_payload={"challenge_id": str(challenge.id)},
        )
        return response

    def _wait_for_interactive(
        self,
        job_id: Any,
        service_id: str,
        result: Any,
        *,
        challenge_kind: str,
    ) -> bool:
        current = self.store.get_job(job_id, owner=self.config.owner_id)
        challenge_type, schema = _challenge_contract(
            service_id,
            challenge_kind=challenge_kind,
        )
        challenge = Challenge.new(
            job_id=current.id,
            type=challenge_type,
            message=str(
                getattr(getattr(result, "challenge", None), "message", "")
                or "iPhoneから同じGoogle Chromeの追加認証を完了してください。"
            ),
            input_schema=schema,
            metadata={"service_id": service_id},
            expires_at=utc_now() + timedelta(seconds=self.config.challenge_ttl_seconds),
        )
        viewer_error = ""
        with self._viewer_lock:
            browser = self._active_browser
            if browser is None:
                viewer_error = "VIEWER_UNAVAILABLE"
            else:
                try:
                    target = browser.current_page_target()
                    self._assert_official_viewer_target(
                        service_id=service_id,
                        target=target,
                    )
                    target_id = str(target.get("targetId") or "")
                    if not target_id:
                        raise ViewerUnavailableError(
                            "追加認証を開始したGoogle Chromeタブを確認できません。"
                        )
                except Exception:
                    viewer_error = "VIEWER_ORIGIN_NOT_ALLOWED"
                else:
                    self._interactive_service_id = service_id
                    self._interactive_target_id = target_id
        if viewer_error:
            self._set_intervention(
                job_id,
                code=viewer_error,
                message=(
                    "同じGoogle Chromeの追加認証画面を確認できません。"
                    if viewer_error == "VIEWER_UNAVAILABLE"
                    else "公式サイト以外の画面はiPhoneから操作できないため、安全に停止しました。"
                ),
            )
            return False
        self.store.add_challenge(challenge, owner=self.config.owner_id)
        with self._viewer_lock:
            self._interactive_challenge_id = str(challenge.id)
            self._interactive_continue.clear()
        waiting = self.store.compare_and_set(
            current.id,
            owner=self.config.owner_id,
            expected_version=current.version,
            state=BatchJobState.WAITING_FOR_CHALLENGE,
            current=service_id,
            event_type="interactive_challenge_waiting",
            event_payload={
                "challenge_id": str(challenge.id),
                "challenge_type": challenge.type.value,
                "service_id": service_id,
            },
        )

        while not self._stop_event.is_set() and utc_now() < challenge.expires_at:
            if self._interactive_continue.wait(timeout=0.5):
                latest = self.store.get_job(waiting.id, owner=self.config.owner_id)
                if latest.state is BatchJobState.CANCELLED:
                    return False
                self.store.compare_and_set(
                    latest.id,
                    owner=self.config.owner_id,
                    expected_version=latest.version,
                    state=BatchJobState.RUNNING,
                    current=service_id,
                    event_type="interactive_challenge_completed",
                    event_payload={"challenge_id": str(challenge.id)},
                )
                with self._viewer_lock:
                    self._interactive_challenge_id = ""
                    self._interactive_service_id = ""
                    self._interactive_target_id = ""
                    self._interactive_continue.clear()
                return True
            latest = self.store.get_job(waiting.id, owner=self.config.owner_id)
            if latest.state is BatchJobState.CANCELLED:
                return False

        latest = self.store.get_job(waiting.id, owner=self.config.owner_id)
        if self._stop_event.is_set():
            # Keep WAITING_FOR_CHALLENGE durable so a replacement worker can
            # requeue the provider from its persistent Chrome profile.
            return False
        if latest.state is BatchJobState.CANCELLED:
            return False
        self.store.compare_and_set(
            latest.id,
            owner=self.config.owner_id,
            expected_version=latest.version,
            state=BatchJobState.FAILED,
            error={
                "code": "INTERACTIVE_CHALLENGE_EXPIRED",
                "message": "追加認証画面の操作期限が切れたため、この試行を終了しました。",
                "retryable": True,
            },
            event_type="interactive_challenge_expired",
            event_payload={"challenge_id": str(challenge.id)},
        )
        return False

    def _finish_failed_result(
        self,
        job_id: Any,
        service_id: str,
        result: Any,
    ) -> bool:
        """Persist a provider result and report whether the batch may continue."""

        failure = getattr(result, "failure", None)
        raw_code = str(getattr(failure, "code", "") or "ACQUISITION_FAILED")
        code = _public_failure_code(raw_code)
        if code == "SECURITY_CHALLENGE":
            self._set_intervention(
                job_id,
                code=code,
                message="未確認の追加認証が表示されたため、安全に停止しました。",
            )
            return False
        current = self.store.get_job(job_id, owner=self.config.owner_id)
        if current.state is BatchJobState.CANCELLED:
            return False
        if _is_non_continuable_failure(code):
            self.store.compare_and_set(
                current.id,
                owner=self.config.owner_id,
                expected_version=current.version,
                state=BatchJobState.FAILED,
                error={
                    "code": code,
                    "message": "安全に続行できないため、自動取得を停止しました。",
                    "retryable": True,
                },
                event_type="job_failed",
                event_payload={"error_code": code, "service_id": service_id},
            )
            return False

        failed_service_ids, service_failures = _service_failure_result(current.result)
        failed_service_ids = tuple(
            dict.fromkeys((*failed_service_ids, service_id))
        )
        service_failures[service_id] = {
            "code": code,
            "message": "このサービスの自動取得を完了できませんでした。",
            "retryable": True,
        }
        next_service = next(
            (
                candidate
                for candidate in current.service_ids
                if candidate not in current.completed
                and candidate not in failed_service_ids
            ),
            None,
        )
        self.store.compare_and_set(
            current.id,
            owner=self.config.owner_id,
            expected_version=current.version,
            state=BatchJobState.RUNNING,
            current=next_service,
            error=None,
            result=_batch_result(
                completed=current.completed,
                failed_service_ids=failed_service_ids,
                service_failures=service_failures,
            ),
            event_type="service_failed",
            event_payload={"service_id": service_id, "error_code": code},
        )
        return True

    def _set_intervention(self, job_id: Any, *, code: str, message: str) -> None:
        current = self.store.get_job(job_id, owner=self.config.owner_id)
        if current.state is BatchJobState.CANCELLED:
            return
        self.store.compare_and_set(
            current.id,
            owner=self.config.owner_id,
            expected_version=current.version,
            state=BatchJobState.INTERVENTION_REQUIRED,
            error={"code": code, "message": message, "retryable": False},
            event_type="intervention_required",
            event_payload={"error_code": code},
        )

    def _fail_unexpected(self, job_id: Any) -> None:
        try:
            current = self.store.get_job(job_id, owner=self.config.owner_id)
            if current.state in {
                BatchJobState.SUCCEEDED,
                BatchJobState.FAILED,
                BatchJobState.INTERVENTION_REQUIRED,
                BatchJobState.CANCELLED,
            }:
                return
            self.store.compare_and_set(
                current.id,
                owner=self.config.owner_id,
                expected_version=current.version,
                state=BatchJobState.FAILED,
                error={
                    "code": "WORKER_UNEXPECTED_ERROR",
                    "message": "常設ワーカーで予期しないエラーが発生しました。",
                    "retryable": True,
                },
                event_type="job_failed",
                event_payload={"error_code": "WORKER_UNEXPECTED_ERROR"},
            )
        except Exception as error:
            LOGGER.error(
                "Could not persist unexpected worker failure (%s)",
                type(error).__name__,
            )

    def _remove_attempt_dir(self, attempt_dir: Path) -> None:
        root = self.config.download_root
        target = Path(attempt_dir).resolve()
        if target == root or root not in target.parents:
            LOGGER.error("Refusing to delete download path outside worker root")
            return
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    def _cancellation_requested(self, job_id: Any) -> bool:
        try:
            current = self.store.get_job(job_id, owner=self.config.owner_id)
        except Exception as error:
            LOGGER.error(
                "Could not verify worker job cancellation state (%s)",
                type(error).__name__,
            )
            return True
        return current.state is BatchJobState.CANCELLED

    def _require_active_browser(
        self,
        job_id: str,
        challenge_id: str,
    ) -> ManagedBrowser:
        if (
            self._active_browser is None
            or not self._active_browser_job_id
            or not hmac.compare_digest(
                self._active_browser_job_id.encode(),
                str(job_id or "").encode(),
            )
            or not self._interactive_challenge_id
            or not hmac.compare_digest(
                self._interactive_challenge_id.encode(),
                str(challenge_id or "").encode(),
            )
            or not self._interactive_service_id
            or not self._interactive_target_id
        ):
            raise ViewerUnavailableError("操作できる取得ブラウザはありません。")
        target = self._active_browser.current_page_target()
        if not hmac.compare_digest(
            str(target.get("targetId") or "").encode(),
            self._interactive_target_id.encode(),
        ):
            raise ViewerUnavailableError("追加認証を開始したGoogle Chromeタブではありません。")
        self._assert_official_viewer_target(
            service_id=self._interactive_service_id,
            target=target,
        )
        return self._active_browser

    @staticmethod
    def _assert_official_viewer_target(
        *,
        service_id: str,
        target: Mapping[str, Any],
    ) -> None:
        parsed = urlsplit(str(target.get("url") or ""))
        hostname = (parsed.hostname or "").lower()
        allowed_hosts = _VIEWER_ALLOWED_HOSTS.get(service_id, frozenset())
        if (
            parsed.scheme.lower() != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or hostname not in allowed_hosts
        ):
            raise ViewerUnavailableError("公式サイト以外の画面は操作できません。")

    @staticmethod
    def _service_id(value: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_SERVICE_ID.fullmatch(normalized):
            raise ValueError("invalid service_id")
        return normalized


def _public_failure_code(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", str(value or "").strip().upper())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:64] or "ACQUISITION_FAILED"


def _is_non_continuable_failure(code: str) -> bool:
    normalized = _public_failure_code(code)
    return (
        normalized in _NON_CONTINUABLE_FAILURE_CODES
        or normalized.startswith(_NON_CONTINUABLE_FAILURE_PREFIXES)
        or any(marker in normalized for marker in _NON_CONTINUABLE_FAILURE_MARKERS)
    )


def _service_failure_result(
    result: Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    if not isinstance(result, Mapping):
        return (), {}
    raw_failures = result.get("service_failures")
    failure_map = raw_failures if isinstance(raw_failures, Mapping) else {}
    failed: list[str] = []
    sanitized: dict[str, dict[str, Any]] = {}
    for raw_service_id in result.get("failed_service_ids") or ():
        service_id = str(raw_service_id or "").strip()
        if (
            not _SAFE_SERVICE_ID.fullmatch(service_id)
            or service_id in failed
        ):
            continue
        failed.append(service_id)
        raw_failure = failure_map.get(service_id)
        failure = raw_failure if isinstance(raw_failure, Mapping) else {}
        sanitized[service_id] = {
            "code": _public_failure_code(str(failure.get("code") or "")),
            "message": "このサービスの自動取得を完了できませんでした。",
            "retryable": bool(failure.get("retryable", True)),
        }
    return tuple(failed), sanitized


def _batch_result(
    *,
    completed: tuple[str, ...],
    failed_service_ids: tuple[str, ...],
    service_failures: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "completed_service_ids": list(completed),
        "failed_service_ids": list(failed_service_ids),
        "service_failures": {
            service_id: {
                "code": _public_failure_code(str(failure.get("code") or "")),
                "message": "このサービスの自動取得を完了できませんでした。",
                "retryable": bool(failure.get("retryable", True)),
            }
            for service_id, failure in service_failures.items()
            if service_id in failed_service_ids
        },
    }


def _challenge_contract(
    service_id: str,
    *,
    challenge_kind: str,
) -> tuple[ChallengeType, ChallengeInputSchema]:
    if challenge_kind != "verification_code":
        challenge_types = {
            "captcha": ChallengeType.CAPTCHA_INTERACTIVE,
            "interactive": ChallengeType.CONSENT_INTERACTIVE,
            "consent": ChallengeType.CONSENT_INTERACTIVE,
            "push_approval": ChallengeType.PUSH_APPROVAL,
            "passkey_unavailable": ChallengeType.PASSKEY_UNAVAILABLE,
        }
        return (
            challenge_types.get(challenge_kind, ChallengeType.UNKNOWN),
            ChallengeInputSchema(
                input_type=ChallengeInputType.REMOTE_BROWSER,
                label="同じGoogle ChromeをiPhoneから操作",
                required=False,
            ),
        )
    if service_id == "epos":
        return (
            ChallengeType.SECURITY_CODE,
            ChallengeInputSchema(
                input_type=ChallengeInputType.CODE,
                label="カード裏面などに記載された3桁のセキュリティコード",
                required=True,
                min_length=3,
                max_length=3,
                pattern=r"^[0-9]{3}$",
                autocomplete="one-time-code",
            ),
        )
    if service_id == "commufa":
        return (
            ChallengeType.OTP_EMAIL,
            ChallengeInputSchema(
                input_type=ChallengeInputType.CODE,
                label="メールに届いた6桁の確認コード",
                required=True,
                min_length=6,
                max_length=6,
                pattern=r"^[0-9]{6}$",
                autocomplete="one-time-code",
            ),
        )
    if service_id == "mobile":
        return (
            ChallengeType.OTP_EMAIL,
            ChallengeInputSchema(
                input_type=ChallengeInputType.CODE,
                label="メールまたはSMSに届いた確認コード",
                required=True,
                min_length=4,
                max_length=8,
                pattern=r"^[0-9]{4,8}$",
                autocomplete="one-time-code",
            ),
        )
    return (
        ChallengeType.OTP_EMAIL,
        ChallengeInputSchema(
            input_type=ChallengeInputType.CODE,
            label="確認コード",
            required=True,
            min_length=4,
            max_length=8,
            pattern=r"^[0-9]{4,8}$",
            autocomplete="one-time-code",
        ),
    )
