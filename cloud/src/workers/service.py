from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..config import parse_month_key, service_by_id
from ..domain.acquisition import AcquisitionOutcome
from ..jobs.inbox import ChallengeResponseInbox
from ..jobs.models import (
    BatchJob,
    BatchJobState,
    Challenge,
    ChallengeInputType,
    ChallengeType,
)
from ..jobs.store import JobNotFoundError, SQLiteJobStore, VersionConflictError
from ..workflows.manual_upload import (
    MAX_MANUAL_PDF_BYTES,
    ManualUploadError,
    save_manual_receipt as save_manual_receipt_workflow,
)
from .runner import ReceiptWorker, ViewerUnavailableError


ACTIVE_STATES = {
    BatchJobState.QUEUED,
    BatchJobState.RUNNING,
    BatchJobState.WAITING_FOR_CHALLENGE,
}
TERMINAL_STATES = {
    BatchJobState.SUCCEEDED,
    BatchJobState.FAILED,
    BatchJobState.INTERVENTION_REQUIRED,
    BatchJobState.CANCELLED,
}
INTERACTIVE_CHALLENGE_TYPES = {
    ChallengeType.CAPTCHA_INTERACTIVE,
    ChallengeType.CONSENT_INTERACTIVE,
    ChallengeType.PASSKEY_UNAVAILABLE,
    ChallengeType.PUSH_APPROVAL,
}


class WorkerServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _CommitGuardedStorage:
    """Use the worker's Drive commit lock without changing validation semantics."""

    def __init__(self, *, storage: Any, worker: ReceiptWorker) -> None:
        self._storage = storage
        self._worker = worker

    def list_files(self) -> Any:
        return self._storage.list_files()

    def upsert_bytes(self, **kwargs: Any) -> Any:
        with self._worker.receipt_commit_guard():
            return self._storage.upsert_bytes(**kwargs)


class WorkerService:
    def __init__(
        self,
        *,
        store: SQLiteJobStore,
        inbox: ChallengeResponseInbox,
        worker: ReceiptWorker,
        owner_id: str,
        microsoft_oauth: Any | None = None,
    ) -> None:
        self.store = store
        self.inbox = inbox
        self.worker = worker
        self.owner_id = str(owner_id or "").strip()
        self.microsoft_oauth = microsoft_oauth
        if not self.owner_id:
            raise ValueError("owner_id is required")

    def authorize_owner(self, owner_id: str) -> None:
        """Authenticate the owner before accepting a potentially large body."""

        self._require_owner(owner_id)

    def create_job(
        self,
        *,
        owner_id: str,
        target_month: str,
        service_ids: Sequence[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_owner(owner_id)
        normalized_services = tuple(str(value) for value in service_ids)
        for service_id in normalized_services:
            try:
                service_by_id(service_id)
            except KeyError as error:
                raise WorkerServiceError(
                    "未対応の取得先が指定されました。",
                    code="INVALID_SERVICE",
                    status_code=400,
                ) from error
        job = self.store.create_job(
            owner=self.owner_id,
            target_month=target_month,
            service_ids=normalized_services,
            idempotency_key=idempotency_key,
        )
        return self.public_job(job)

    def get_job(self, job_id: str, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        return self.public_job(self._job(job_id))

    def find_active_job(self, *, target_month: str, owner_id: str) -> dict[str, Any] | None:
        self._require_owner(owner_id)
        job = self.store.find_active_job(
            owner=self.owner_id,
            target_month=target_month,
        )
        return self.public_job(job) if job is not None else None

    def save_manual_receipt(
        self,
        *,
        owner_id: str,
        service_id: str,
        target_month: str,
        content: bytes,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Validate and persist one iPhone upload inside the receipt worker."""

        self._require_owner(owner_id)
        normalized_service_id = str(service_id or "").strip()
        normalized_month = str(target_month or "").strip()
        try:
            service_by_id(normalized_service_id)
            parse_month_key(normalized_month)
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerServiceError(
                "手動保存の対象サービスまたは対象月が不正です。",
                code="MANUAL_UPLOAD_INVALID",
                status_code=400,
            ) from error
        if not isinstance(content, bytes) or len(content) > MAX_MANUAL_PDF_BYTES:
            raise WorkerServiceError(
                "PDFは20MiB以下にしてください。",
                code="MANUAL_UPLOAD_TOO_LARGE",
                status_code=413,
            )

        active = self.store.find_active_job(
            owner=self.owner_id,
            target_month=normalized_month,
        )
        if active is not None or self.worker.active_job_id:
            raise WorkerServiceError(
                "自動取得を停止または完了してから手動PDFを保存してください。",
                code="MANUAL_UPLOAD_BUSY",
                status_code=409,
            )

        storage = _CommitGuardedStorage(
            storage=self.worker.storage_factory(),
            worker=self.worker,
        )
        try:
            result = save_manual_receipt_workflow(
                service_id=normalized_service_id,
                target_month=normalized_month,
                content=content,
                original_file_name="iphone-upload.pdf",
                storage=storage,
                confirmed=bool(confirmed),
            )
        except ManualUploadError as error:
            raise WorkerServiceError(
                "PDFの内容を確認できませんでした。対象と確認設定を見直してください。",
                code="MANUAL_UPLOAD_INVALID",
                status_code=400,
            ) from error

        if not bool(getattr(result, "success", False)):
            raise WorkerServiceError(
                "Google Driveへの手動保存を完了できませんでした。",
                code="MANUAL_UPLOAD_SAVE_FAILED",
                status_code=502,
            )
        receipt = getattr(result, "receipt", None)
        outcome = getattr(result, "outcome", None)
        file_id = str(getattr(receipt, "file_id", "") or "")
        file_name = str(getattr(receipt, "file_name", "") or "")
        if not file_id or not file_name:
            raise WorkerServiceError(
                "Google Driveへの手動保存を確認できませんでした。",
                code="MANUAL_UPLOAD_SAVE_FAILED",
                status_code=502,
            )
        return {
            "success": True,
            "service_id": normalized_service_id,
            "target_month": normalized_month,
            "status": str(getattr(outcome, "value", "") or ""),
            "skipped": outcome is AcquisitionOutcome.ALREADY_EXISTS,
            "receipt": {
                "file_id": file_id,
                "file_name": file_name,
                "web_view_link": str(
                    getattr(receipt, "web_view_link", "") or ""
                ),
            },
        }

    def submit_challenge_response(
        self,
        *,
        job_id: str,
        challenge_id: str,
        owner_id: str,
        response: str,
    ) -> dict[str, Any]:
        self._require_owner(owner_id)
        job = self._job(job_id)
        if job.state is not BatchJobState.WAITING_FOR_CHALLENGE:
            raise WorkerServiceError(
                "このジョブは追加認証を待っていません。",
                code="CHALLENGE_NOT_PENDING",
                status_code=409,
            )
        challenge = self._latest_challenge(job)
        if challenge is None or str(challenge.id) != str(challenge_id):
            raise WorkerServiceError(
                "追加認証が更新されました。最新状態を読み直してください。",
                code="CHALLENGE_REPLACED",
                status_code=409,
            )
        if challenge.input_schema.input_type not in {
            ChallengeInputType.CODE,
            ChallengeInputType.TEXT,
        }:
            raise WorkerServiceError(
                "この追加認証は文字入力ではなく取得ブラウザ上で操作してください。",
                code="CHALLENGE_RESPONSE_NOT_SUPPORTED",
                status_code=409,
            )
        normalized = _validate_response(challenge, response)
        try:
            self.inbox.submit(
                challenge_id=str(challenge.id),
                owner_id=self.owner_id,
                response=normalized,
            )
        except Exception as error:
            code = str(getattr(error, "code", "") or "CHALLENGE_NOT_PENDING")
            raise WorkerServiceError(
                str(error) or "追加認証を受け付けられませんでした。",
                code=code,
                status_code=409,
            ) from error
        finally:
            normalized = ""
        return self.public_job(job)

    def viewer_frame(
        self,
        *,
        job_id: str,
        challenge_id: str,
        owner_id: str,
    ) -> bytes:
        self._require_owner(owner_id)
        self._interactive_challenge(job_id, challenge_id)
        try:
            return self.worker.capture_viewer_frame(job_id, challenge_id)
        except ViewerUnavailableError as error:
            raise WorkerServiceError(
                str(error),
                code="VIEWER_UNAVAILABLE",
                status_code=409,
            ) from error

    def send_viewer_input(
        self,
        *,
        job_id: str,
        challenge_id: str,
        owner_id: str,
        action: str,
        x: int | None = None,
        y: int | None = None,
        text: str = "",
        key: str = "",
    ) -> dict[str, Any]:
        self._require_owner(owner_id)
        job, _challenge = self._interactive_challenge(job_id, challenge_id)
        try:
            self.worker.send_viewer_input(
                job_id,
                challenge_id,
                action=action,
                x=x,
                y=y,
                text=text,
                key=key,
            )
        except ViewerUnavailableError as error:
            raise WorkerServiceError(
                str(error),
                code="VIEWER_UNAVAILABLE",
                status_code=409,
            ) from error
        except ValueError as error:
            raise WorkerServiceError(
                str(error),
                code="VIEWER_INPUT_INVALID",
                status_code=400,
            ) from error
        finally:
            text = ""
        return self.public_job(job)

    def complete_interactive_challenge(
        self,
        *,
        job_id: str,
        challenge_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        self._require_owner(owner_id)
        job, _challenge = self._interactive_challenge(job_id, challenge_id)
        try:
            self.worker.continue_interactive(job_id, challenge_id)
        except ViewerUnavailableError as error:
            raise WorkerServiceError(
                str(error),
                code="VIEWER_UNAVAILABLE",
                status_code=409,
            ) from error
        return self.public_job(job)

    def cancel_job(self, job_id: str, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        with self.worker.receipt_commit_guard():
            while True:
                job = self._job(job_id)
                if job.state in TERMINAL_STATES:
                    return self.public_job(job)
                try:
                    cancelled = self.store.compare_and_set(
                        job.id,
                        owner=self.owner_id,
                        expected_version=job.version,
                        state=BatchJobState.CANCELLED,
                        current=None,
                        error={
                            "code": "CANCELLED_BY_OWNER",
                            "message": "iPhoneから自動取得を中止しました。",
                            "retryable": True,
                        },
                        event_type="job_cancelled",
                        event_payload={"reason_code": "CANCELLED_BY_OWNER"},
                    )
                except VersionConflictError:
                    # A worker transition won this version. Reload and either
                    # cancel the new active version or return its terminal state.
                    continue
                challenge = (
                    self._latest_challenge(cancelled)
                    if job.state is BatchJobState.WAITING_FOR_CHALLENGE
                    else None
                )
                if challenge is not None:
                    self.inbox.discard(str(challenge.id))
                break
        self.worker.notify_job_cancelled(str(job.id))
        return self.public_job(cancelled)

    def health(self, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        return {
            "status": "ok" if self.worker.running else "starting",
            "worker_running": self.worker.running,
            "active_job_id": self.worker.active_job_id,
            "browser_family": "Google Chrome",
            "time": datetime.now(timezone.utc).isoformat(),
        }

    def microsoft_oauth_status(self, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        if self.microsoft_oauth is None:
            return {"configured": False, "connected": False, "updated_at": ""}
        return dict(self.microsoft_oauth.status())

    def start_microsoft_oauth(self, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        manager = self._require_microsoft_oauth()
        try:
            payload = dict(manager.start())
        except Exception as error:
            raise self._microsoft_oauth_error(error) from error
        authorization_url = str(payload.get("authorization_url") or "")
        parsed = urlsplit(authorization_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "login.microsoftonline.com"
        ):
            raise WorkerServiceError(
                "Microsoft公式の認証URLを確認できませんでした。",
                code="MICROSOFT_AUTHORIZATION_URL_INVALID",
                status_code=500,
            )
        return payload

    def complete_microsoft_oauth(
        self,
        *,
        owner_id: str,
        code: str,
        state: str,
    ) -> dict[str, Any]:
        self._require_owner(owner_id)
        manager = self._require_microsoft_oauth()
        normalized_code = str(code or "")
        normalized_state = str(state or "")
        try:
            return dict(
                manager.complete(
                    code=normalized_code,
                    state=normalized_state,
                )
            )
        except Exception as error:
            raise self._microsoft_oauth_error(error) from error
        finally:
            normalized_code = ""
            normalized_state = ""
            code = ""
            state = ""

    def disconnect_microsoft_oauth(self, *, owner_id: str) -> dict[str, Any]:
        self._require_owner(owner_id)
        manager = self._require_microsoft_oauth()
        try:
            manager.disconnect()
        except Exception as error:
            raise self._microsoft_oauth_error(error) from error
        return dict(manager.status())

    def public_job(self, job: BatchJob) -> dict[str, Any]:
        challenge = (
            self._latest_challenge(job)
            if job.state is BatchJobState.WAITING_FOR_CHALLENGE
            else None
        )
        failed_service_ids, service_failures = _public_service_failures(job)
        public_error = None
        if isinstance(job.error, Mapping):
            public_error = {
                "code": str(job.error.get("code") or job.error.get("error_code") or ""),
                "message": str(job.error.get("message") or "自動取得を完了できませんでした。"),
                "retryable": bool(job.error.get("retryable", False)),
            }
        public_result = dict(job.result) if isinstance(job.result, Mapping) else None
        if public_result is not None:
            public_result["failed_service_ids"] = list(failed_service_ids)
            public_result["service_failures"] = service_failures
        return {
            "id": str(job.id),
            "target_month": job.target_month,
            "service_ids": list(job.service_ids),
            "completed_service_ids": list(job.completed),
            "failed_service_ids": list(failed_service_ids),
            "service_failures": service_failures,
            "current_service_id": job.current,
            "state": job.state.value,
            "version": job.version,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "challenge": _public_challenge(challenge),
            "error": public_error,
            "result": public_result,
            "worker": {
                "running": self.worker.running,
                "active_job_id": self.worker.active_job_id,
            },
        }

    def _latest_challenge(self, job: BatchJob) -> Challenge | None:
        challenges = self.store.list_challenges(job.id, owner=self.owner_id)
        return challenges[-1] if challenges else None

    def _interactive_challenge(
        self,
        job_id: str,
        challenge_id: str,
    ) -> tuple[BatchJob, Challenge]:
        job = self._job(job_id)
        if job.state is not BatchJobState.WAITING_FOR_CHALLENGE:
            raise WorkerServiceError(
                "このジョブは追加認証を待っていません。",
                code="CHALLENGE_NOT_PENDING",
                status_code=409,
            )
        challenge = self._latest_challenge(job)
        if challenge is None or str(challenge.id) != str(challenge_id):
            raise WorkerServiceError(
                "追加認証が更新されました。最新状態を読み直してください。",
                code="CHALLENGE_REPLACED",
                status_code=409,
            )
        if challenge.type not in INTERACTIVE_CHALLENGE_TYPES:
            raise WorkerServiceError(
                "この追加認証には取得ブラウザ操作を利用できません。",
                code="VIEWER_NOT_ALLOWED",
                status_code=409,
            )
        return job, challenge

    def _job(self, job_id: str) -> BatchJob:
        try:
            return self.store.get_job(job_id, owner=self.owner_id)
        except (JobNotFoundError, ValueError) as error:
            raise WorkerServiceError(
                "ジョブが見つかりません。",
                code="JOB_NOT_FOUND",
                status_code=404,
            ) from error

    def _require_owner(self, owner_id: str) -> None:
        import hmac

        actual = str(owner_id or "")
        if not hmac.compare_digest(actual.encode(), self.owner_id.encode()):
            raise WorkerServiceError(
                "このジョブを操作する権限がありません。",
                code="OWNER_FORBIDDEN",
                status_code=403,
            )

    def _require_microsoft_oauth(self) -> Any:
        if self.microsoft_oauth is None:
            raise WorkerServiceError(
                "Microsoft OAuthがワーカーに設定されていません。",
                code="MICROSOFT_OAUTH_NOT_CONFIGURED",
                status_code=503,
            )
        return self.microsoft_oauth

    @staticmethod
    def _microsoft_oauth_error(error: Exception) -> WorkerServiceError:
        code = str(
            getattr(error, "code", "")
            or "MICROSOFT_OAUTH_FAILED"
        )
        public_messages = {
            "MICROSOFT_OAUTH_REQUIRED": "Microsoftメールが未接続です。",
            "MICROSOFT_OAUTH_STATE_EXPIRED": (
                "Microsoft接続の有効期限が切れました。最初からやり直してください。"
            ),
            "MICROSOFT_OAUTH_RESPONSE_INVALID": "Microsoft認証応答の形式が不正です。",
            "MICROSOFT_OAUTH_REJECTED": "Microsoft認証を完了できませんでした。",
            "MICROSOFT_OAUTH_UNREACHABLE": "Microsoft認証サーバーへ接続できませんでした。",
            "MICROSOFT_OAUTH_RECONNECT_REQUIRED": (
                "Microsoftメールを再接続してください。"
            ),
            "MICROSOFT_TOKEN_DECRYPT_FAILED": (
                "Microsoftメールを再接続してください。"
            ),
        }
        status_codes = {
            "MICROSOFT_OAUTH_REQUIRED": 409,
            "MICROSOFT_OAUTH_STATE_EXPIRED": 400,
            "MICROSOFT_OAUTH_RESPONSE_INVALID": 400,
            "MICROSOFT_OAUTH_REJECTED": 400,
            "MICROSOFT_OAUTH_RECONNECT_REQUIRED": 409,
            "MICROSOFT_TOKEN_DECRYPT_FAILED": 409,
            "MICROSOFT_OAUTH_UNREACHABLE": 503,
        }
        return WorkerServiceError(
            public_messages.get(code, "Microsoftメール接続を完了できませんでした。"),
            code=code,
            status_code=status_codes.get(code, 503),
        )


def _public_service_failures(
    job: BatchJob,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    result = job.result if isinstance(job.result, Mapping) else {}
    raw_failures = result.get("service_failures")
    failure_map = raw_failures if isinstance(raw_failures, Mapping) else {}
    failed: list[str] = []
    failures: dict[str, dict[str, Any]] = {}
    for raw_service_id in result.get("failed_service_ids") or ():
        service_id = str(raw_service_id or "").strip()
        if service_id not in job.service_ids or service_id in failed:
            continue
        failed.append(service_id)
        raw_failure = failure_map.get(service_id)
        failure = raw_failure if isinstance(raw_failure, Mapping) else {}
        code = re.sub(
            r"[^A-Z0-9_]",
            "_",
            str(failure.get("code") or "ACQUISITION_FAILED").upper(),
        )
        failures[service_id] = {
            "code": code[:64] or "ACQUISITION_FAILED",
            "message": "このサービスの自動取得を完了できませんでした。",
            "retryable": bool(failure.get("retryable", True)),
        }
    return tuple(failed), failures


def _public_challenge(challenge: Challenge | None) -> dict[str, Any] | None:
    if challenge is None:
        return None
    return {
        "id": str(challenge.id),
        "kind": challenge.type.value,
        "message": challenge.message,
        "input_schema": challenge.input_schema.to_dict(),
        "masked_destination": str(challenge.metadata.get("masked_destination") or ""),
        "viewer_url": str(challenge.metadata.get("viewer_url") or ""),
        "viewer_available": challenge.type in INTERACTIVE_CHALLENGE_TYPES,
        "expires_at": challenge.expires_at.isoformat() if challenge.expires_at else None,
    }


def _validate_response(challenge: Challenge, value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    schema = challenge.input_schema
    minimum = schema.min_length if schema.min_length is not None else (1 if schema.required else 0)
    maximum = schema.max_length if schema.max_length is not None else 128
    if not minimum <= len(normalized) <= maximum:
        raise WorkerServiceError(
            f"入力は{minimum}〜{maximum}文字で確認してください。",
            code="CHALLENGE_RESPONSE_INVALID",
            status_code=400,
        )
    if schema.pattern and re.fullmatch(schema.pattern, normalized) is None:
        raise WorkerServiceError(
            "追加認証の入力形式を確認してください。",
            code="CHALLENGE_RESPONSE_INVALID",
            status_code=400,
        )
    return normalized
