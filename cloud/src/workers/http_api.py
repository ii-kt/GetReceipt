from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from ..jobs.client import bearer_token_matches
from ..jobs.store import IdempotencyConflictError, VersionConflictError
from ..workflows.manual_upload import MAX_MANUAL_PDF_BYTES
from .service import WorkerService, WorkerServiceError


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MAX_BODY_BYTES = 16 * 1024
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
LOGGER = logging.getLogger(__name__)


class _RequestBodyTooLarge(ValueError):
    pass


class WorkerASGIApp:
    """Small authenticated ASGI surface for the personal receipt worker.

    Streamlit calls this server-to-server, so no CORS or browser bearer token is
    needed. Request bodies are capped and never logged here.
    """

    def __init__(
        self,
        *,
        service: WorkerService,
        api_token: str,
        shutdown_callback: Callable[[], None] | None = None,
        backup_lifecycle: Any | None = None,
    ) -> None:
        token = str(api_token or "").strip()
        if len(token) < 32:
            raise ValueError("api_token must be at least 32 characters")
        self.service = service
        self.api_token = token
        self.shutdown_callback = shutdown_callback
        self.backup_lifecycle = backup_lifecycle

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            await self._json(send, 404, error=_error("NOT_FOUND", "Not found."))
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        if not bearer_token_matches(headers.get("authorization", ""), self.api_token):
            await self._json(
                send,
                401,
                error=_error("UNAUTHORIZED", "取得ワーカーの認証に失敗しました。"),
            )
            return
        owner_id = str(headers.get("x-getreceipt-owner", ""))
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))

        try:
            if method == "GET" and path == "/healthz":
                payload = self.service.health(owner_id=owner_id)
                status_code = 200
                if self.backup_lifecycle is not None:
                    payload["sqlite_backup"] = self.backup_lifecycle.health()
                    if not self.backup_lifecycle.healthy:
                        payload["status"] = (
                            "fatal"
                            if payload["sqlite_backup"].get("fatal")
                            else "degraded"
                        )
                        status_code = 503
                await self._json(send, status_code, payload=payload)
                return

            if method == "GET" and path == "/v1/oauth/microsoft/status":
                payload = self.service.microsoft_oauth_status(owner_id=owner_id)
                await self._json(send, 200, payload=payload)
                return

            if method == "POST" and path == "/v1/oauth/microsoft/start":
                await self._discard_body(receive)
                payload = self.service.start_microsoft_oauth(owner_id=owner_id)
                await self._json(send, 200, payload=payload)
                return

            if method == "POST" and path == "/v1/oauth/microsoft/complete":
                body = await self._body_json(receive)
                payload = self.service.complete_microsoft_oauth(
                    owner_id=owner_id,
                    code=str(body.get("code") or ""),
                    state=str(body.get("state") or ""),
                )
                await self._json(send, 200, payload=payload)
                return

            if method == "POST" and path == "/v1/oauth/microsoft/disconnect":
                await self._discard_body(receive)
                payload = self.service.disconnect_microsoft_oauth(owner_id=owner_id)
                await self._json(send, 200, payload=payload)
                return

            if method == "POST" and path == "/v1/manual-receipts":
                # Authenticate both credentials before accepting up to 20MiB.
                self.service.authorize_owner(owner_id)
                media_type = str(headers.get("content-type") or "").split(";", 1)[0]
                if media_type.strip().lower() != "application/pdf":
                    raise WorkerServiceError(
                        "PDFをapplication/pdfで送信してください。",
                        code="MANUAL_UPLOAD_MEDIA_TYPE_INVALID",
                        status_code=415,
                    )
                content_encoding = str(headers.get("content-encoding") or "").strip().lower()
                if content_encoding not in {"", "identity"}:
                    raise WorkerServiceError(
                        "圧縮されたアップロード本文は利用できません。",
                        code="MANUAL_UPLOAD_CONTENT_ENCODING_INVALID",
                        status_code=415,
                    )
                content_length = str(headers.get("content-length") or "").strip()
                if content_length:
                    declared_length = int(content_length)
                    if declared_length < 0:
                        raise ValueError("negative content length")
                    if declared_length > MAX_MANUAL_PDF_BYTES:
                        raise _RequestBodyTooLarge("manual PDF is too large")
                query = parse_qs(
                    bytes(scope.get("query_string", b"")).decode("utf-8"),
                    keep_blank_values=True,
                )
                service_id = _single_query_value(query, "service_id")
                target_month = _single_query_value(query, "target_month")
                confirmed_value = _single_query_value(query, "confirmed")
                if confirmed_value not in {"true", "false"}:
                    raise ValueError("confirmed must be true or false")
                content = await self._read_body(
                    receive,
                    max_bytes=MAX_MANUAL_PDF_BYTES,
                )
                payload = await asyncio.to_thread(
                    self.service.save_manual_receipt,
                    owner_id=owner_id,
                    service_id=service_id,
                    target_month=target_month,
                    content=content,
                    confirmed=confirmed_value == "true",
                )
                content = b""
                await self._json(send, 200, payload=payload)
                return

            if method == "POST" and path == "/v1/jobs":
                body = await self._body_json(receive)
                payload = self.service.create_job(
                    owner_id=owner_id,
                    target_month=str(body.get("target_month") or ""),
                    service_ids=_string_list(body.get("service_ids")),
                    idempotency_key=str(body.get("idempotency_key") or ""),
                )
                await self._json(send, 200, payload=payload)
                return

            if method == "GET" and path == "/v1/jobs/active":
                query = parse_qs(
                    bytes(scope.get("query_string", b"")).decode("utf-8"),
                    keep_blank_values=True,
                )
                target_month = str((query.get("target_month") or [""])[0])
                payload = self.service.find_active_job(
                    target_month=target_month,
                    owner_id=owner_id,
                )
                if payload is None:
                    await self._json(
                        send,
                        404,
                        error=_error("JOB_NOT_FOUND", "実行中のジョブはありません。"),
                    )
                else:
                    await self._json(send, 200, payload=payload)
                return

            job_match = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_-]{1,200})", path)
            if method == "GET" and job_match:
                payload = self.service.get_job(
                    job_match.group(1),
                    owner_id=owner_id,
                )
                await self._json(send, 200, payload=payload)
                return

            cancel_match = re.fullmatch(
                r"/v1/jobs/([A-Za-z0-9_-]{1,200})/cancel",
                path,
            )
            if method == "POST" and cancel_match:
                await self._discard_body(receive)
                payload = self.service.cancel_job(
                    cancel_match.group(1),
                    owner_id=owner_id,
                )
                await self._json(send, 200, payload=payload)
                return

            challenge_match = re.fullmatch(
                (
                    r"/v1/jobs/([A-Za-z0-9_-]{1,200})"
                    r"/challenges/([A-Za-z0-9_-]{1,200})/respond"
                ),
                path,
            )
            if method == "POST" and challenge_match:
                body = await self._body_json(receive)
                payload = self.service.submit_challenge_response(
                    job_id=challenge_match.group(1),
                    challenge_id=challenge_match.group(2),
                    owner_id=owner_id,
                    response=str(body.get("response") or ""),
                )
                await self._json(send, 200, payload=payload)
                return

            viewer_frame_match = re.fullmatch(
                (
                    r"/v1/jobs/([A-Za-z0-9_-]{1,200})"
                    r"/challenges/([A-Za-z0-9_-]{1,200})/viewer/frame"
                ),
                path,
            )
            if method == "GET" and viewer_frame_match:
                frame = self.service.viewer_frame(
                    job_id=viewer_frame_match.group(1),
                    challenge_id=viewer_frame_match.group(2),
                    owner_id=owner_id,
                )
                await self._png(send, frame)
                return

            viewer_input_match = re.fullmatch(
                (
                    r"/v1/jobs/([A-Za-z0-9_-]{1,200})"
                    r"/challenges/([A-Za-z0-9_-]{1,200})/viewer/input"
                ),
                path,
            )
            if method == "POST" and viewer_input_match:
                body = await self._body_json(receive)
                payload = self.service.send_viewer_input(
                    job_id=viewer_input_match.group(1),
                    challenge_id=viewer_input_match.group(2),
                    owner_id=owner_id,
                    action=str(body.get("action") or ""),
                    x=_optional_int(body.get("x")),
                    y=_optional_int(body.get("y")),
                    text=str(body.get("text") or ""),
                    key=str(body.get("key") or ""),
                )
                await self._json(send, 200, payload=payload)
                return

            viewer_complete_match = re.fullmatch(
                (
                    r"/v1/jobs/([A-Za-z0-9_-]{1,200})"
                    r"/challenges/([A-Za-z0-9_-]{1,200})/viewer/complete"
                ),
                path,
            )
            if method == "POST" and viewer_complete_match:
                await self._discard_body(receive)
                payload = self.service.complete_interactive_challenge(
                    job_id=viewer_complete_match.group(1),
                    challenge_id=viewer_complete_match.group(2),
                    owner_id=owner_id,
                )
                await self._json(send, 200, payload=payload)
                return

            await self._json(
                send,
                404,
                error=_error("NOT_FOUND", "要求されたAPIはありません。"),
            )
        except WorkerServiceError as error:
            await self._json(
                send,
                error.status_code,
                error=_error(error.code, str(error)),
            )
        except IdempotencyConflictError:
            await self._json(
                send,
                409,
                error=_error(
                    "IDEMPOTENCY_CONFLICT",
                    "同じ開始要求が異なる内容で再利用されました。",
                ),
            )
        except VersionConflictError:
            await self._json(
                send,
                409,
                error=_error(
                    "JOB_VERSION_CONFLICT",
                    "ジョブ状態が更新されました。最新状態を読み直してください。",
                ),
            )
        except _RequestBodyTooLarge:
            await self._json(
                send,
                413,
                error=_error(
                    "MANUAL_UPLOAD_TOO_LARGE",
                    "PDFは20MiB以下にしてください。",
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            await self._json(
                send,
                400,
                error=_error("INVALID_REQUEST", "要求の形式が不正です。"),
            )
        except Exception:
            await self._json(
                send,
                500,
                error=_error(
                    "WORKER_INTERNAL_ERROR",
                    "取得ワーカーで内部エラーが発生しました。",
                ),
            )

    async def _lifespan(self, receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                try:
                    if self.backup_lifecycle is not None:
                        self.backup_lifecycle.start()
                    self.service.worker.start()
                except Exception as error:
                    if self.backup_lifecycle is not None:
                        try:
                            self.backup_lifecycle.stop(
                                create_final_backup=False,
                            )
                        except Exception as backup_error:
                            LOGGER.error(
                                "Worker backup startup cleanup failed (%s)",
                                type(backup_error).__name__,
                            )
                    if self.shutdown_callback is not None:
                        try:
                            self.shutdown_callback()
                        except Exception as shutdown_error:
                            LOGGER.error(
                                "Worker startup cleanup failed (%s)",
                                type(shutdown_error).__name__,
                            )
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": f"worker startup failed: {type(error).__name__}",
                        }
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                worker_stopped = False
                try:
                    try:
                        stop_result = self.service.worker.stop()
                        # Legacy/fake workers return None; only an explicit
                        # False means the worker thread may still use SQLite.
                        worker_stopped = stop_result is not False
                    finally:
                        if self.backup_lifecycle is not None:
                            self.backup_lifecycle.stop(
                                create_final_backup=True,
                            )
                finally:
                    try:
                        if self.shutdown_callback is not None and worker_stopped:
                            self.shutdown_callback()
                        elif self.shutdown_callback is not None:
                            LOGGER.critical(
                                "Worker did not stop before shutdown; "
                                "database close was intentionally skipped"
                            )
                    finally:
                        await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _read_body(
        receive: ASGIReceive,
        *,
        max_bytes: int = _MAX_BODY_BYTES,
    ) -> bytes:
        chunks = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") != "http.request":
                continue
            chunk = bytes(message.get("body", b""))
            if len(chunks) + len(chunk) > max_bytes:
                if max_bytes == MAX_MANUAL_PDF_BYTES:
                    raise _RequestBodyTooLarge("manual PDF is too large")
                raise ValueError("request body is too large")
            chunks.extend(chunk)
            more_body = bool(message.get("more_body", False))
        return bytes(chunks)

    async def _body_json(self, receive: ASGIReceive) -> dict[str, Any]:
        raw = await self._read_body(receive)
        decoded = json.loads(raw.decode("utf-8") if raw else "{}")
        if not isinstance(decoded, dict):
            raise ValueError("JSON body must be an object")
        return decoded

    async def _discard_body(self, receive: ASGIReceive) -> None:
        await self._read_body(receive)

    @staticmethod
    async def _json(
        send: ASGISend,
        status: int,
        *,
        payload: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        content = payload if error is None else {"error": error}
        raw = json.dumps(
            content or {},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": int(status),
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"content-length", str(len(raw)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    @staticmethod
    async def _png(send: ASGISend, content: bytes) -> None:
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("viewer frame must be a PNG")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"image/png"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'none'; sandbox"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"content-length", str(len(content)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": content})


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": str(code), "message": str(message)}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("value must be a list")
    normalized = [str(item or "").strip() for item in value]
    if not normalized or any(not _SAFE_ID.fullmatch(item) for item in normalized):
        raise ValueError("invalid identifier list")
    return normalized


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _single_query_value(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    if len(values) != 1:
        raise ValueError(f"{name} must be provided exactly once")
    return str(values[0] or "").strip()
