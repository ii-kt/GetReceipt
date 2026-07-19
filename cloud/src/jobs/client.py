from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests


_SAFE_OWNER_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
_SAFE_MONTH = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_OAUTH_RESPONSE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{20,2048}$")
_MAX_MANUAL_PDF_BYTES = 20 * 1024 * 1024


class WorkerConfigError(RuntimeError):
    pass


class WorkerApiError(RuntimeError):
    """A sanitized worker response error.

    Request bodies and authorization values are deliberately never attached to
    this exception.
    """

    def __init__(self, message: str, *, code: str = "WORKER_API_ERROR", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = str(code or "WORKER_API_ERROR")
        self.status_code = int(status_code or 0)


@dataclass(frozen=True)
class WorkerConnection:
    base_url: str
    api_token: str
    owner_id: str

    def __post_init__(self) -> None:
        normalized_url = str(self.base_url or "").strip().rstrip("/")
        token = str(self.api_token or "").strip()
        owner_id = str(self.owner_id or "").strip()
        parsed = urlsplit(normalized_url)
        local_host = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in ({"http", "https"} if local_host else {"https"}):
            raise WorkerConfigError("ワーカーURLはHTTPSで設定してください。")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WorkerConfigError("ワーカーURLの形式が不正です。")
        if len(token) < 32:
            raise WorkerConfigError("ワーカーAPIトークンは32文字以上で設定してください。")
        if not _SAFE_OWNER_ID.fullmatch(owner_id):
            raise WorkerConfigError("ワーカーowner_idの形式が不正です。")
        object.__setattr__(self, "base_url", normalized_url)
        object.__setattr__(self, "api_token", token)
        object.__setattr__(self, "owner_id", owner_id)


def worker_connection_from_secrets(secrets: Mapping[str, Any]) -> WorkerConnection | None:
    try:
        section = secrets["receipt_worker"]
    except (KeyError, TypeError):
        return None

    def value(name: str) -> str:
        try:
            raw = section.get(name)
        except (AttributeError, TypeError):
            raw = None
        return str(raw or "").strip()

    configured_values = {
        "base_url": value("base_url"),
        "api_token": value("api_token"),
        "owner_id": value("owner_id"),
    }
    if not any(configured_values.values()):
        return None
    if not all(configured_values.values()):
        missing = [name for name, configured in configured_values.items() if not configured]
        raise WorkerConfigError(
            "Streamlit Secretsの [receipt_worker] 設定が不足しています: " + ", ".join(missing)
        )
    return WorkerConnection(**configured_values)


class WorkerClient:
    def __init__(
        self,
        connection: WorkerConnection,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.connection = connection
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def microsoft_oauth_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/oauth/microsoft/status")

    def start_microsoft_oauth(self) -> dict[str, Any]:
        payload = self._request("POST", "/v1/oauth/microsoft/start")
        authorization_url = str(payload.get("authorization_url") or "")
        parsed = urlsplit(authorization_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "login.microsoftonline.com"
        ):
            raise WorkerApiError(
                "Microsoft公式の認証URLを確認できませんでした。",
                code="MICROSOFT_AUTHORIZATION_URL_INVALID",
            )
        return payload

    def complete_microsoft_oauth(self, *, code: str, state: str) -> dict[str, Any]:
        normalized_code = str(code or "").strip()
        normalized_state = str(state or "").strip()
        if not _OAUTH_RESPONSE_VALUE.fullmatch(normalized_code) or not _OAUTH_RESPONSE_VALUE.fullmatch(normalized_state):
            raise ValueError("Microsoft認証応答の形式が不正です。")
        try:
            return self._request(
                "POST",
                "/v1/oauth/microsoft/complete",
                json_body={
                    "code": normalized_code,
                    "state": normalized_state,
                },
            )
        finally:
            normalized_code = ""
            normalized_state = ""
            code = ""
            state = ""

    def disconnect_microsoft_oauth(self) -> dict[str, Any]:
        return self._request("POST", "/v1/oauth/microsoft/disconnect")

    def create_job(
        self,
        *,
        target_month: str,
        service_ids: list[str] | tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        month = self._target_month(target_month)
        services = self._service_ids(service_ids)
        if not _SAFE_IDENTIFIER.fullmatch(str(idempotency_key or "")):
            raise ValueError("idempotency_keyの形式が不正です。")
        return self._request(
            "POST",
            "/v1/jobs",
            json_body={
                "target_month": month,
                "service_ids": services,
                "idempotency_key": str(idempotency_key),
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{self._identifier(job_id, 'job_id')}")

    def find_active_job(self, target_month: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            "/v1/jobs/active",
            query={"target_month": self._target_month(target_month)},
            allow_not_found=True,
        )
        return result or None

    def submit_challenge_response(
        self,
        *,
        job_id: str,
        challenge_id: str,
        response: str,
    ) -> dict[str, Any]:
        # The caller must not retain `response`; this method never interpolates
        # it into a URL, header, exception, or diagnostic.
        normalized_response = str(response or "")
        if not normalized_response or len(normalized_response) > 128:
            raise ValueError("追加認証の入力値が不正です。")
        return self._request(
            "POST",
            (
                f"/v1/jobs/{self._identifier(job_id, 'job_id')}"
                f"/challenges/{self._identifier(challenge_id, 'challenge_id')}/respond"
            ),
            json_body={"response": normalized_response},
        )

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/jobs/{self._identifier(job_id, 'job_id')}/cancel",
        )

    def upload_manual_receipt(
        self,
        *,
        service_id: str,
        target_month: str,
        content: bytes,
        confirmed: bool,
    ) -> dict[str, Any]:
        normalized_service_id = self._identifier(service_id, "service_id")
        month = self._target_month(target_month)
        if not isinstance(confirmed, bool):
            raise ValueError("confirmed must be a boolean")
        if not isinstance(content, bytes) or not content.startswith(b"%PDF"):
            raise ValueError("PDFファイルを選んでください。")
        if len(content) > _MAX_MANUAL_PDF_BYTES:
            raise ValueError("PDFは20MiB以下にしてください。")
        response = self._perform_request(
            "POST",
            "/v1/manual-receipts",
            query={
                "service_id": normalized_service_id,
                "target_month": month,
                "confirmed": "true" if confirmed else "false",
            },
            raw_body=content,
            content_type="application/pdf",
            timeout_seconds=max(self.timeout_seconds, 120.0),
        )
        payload: dict[str, Any] = {}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                payload = decoded
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            self._raise_for_status(response, payload=payload)
        if not payload or payload.get("success") is not True:
            raise WorkerApiError(
                "取得ワーカーから有効な手動保存結果を受け取れませんでした。",
                code="WORKER_INVALID_RESPONSE",
                status_code=response.status_code,
            )
        return payload

    def get_viewer_frame(self, *, job_id: str, challenge_id: str) -> bytes:
        response = self._perform_request(
            "GET",
            (
                f"/v1/jobs/{self._identifier(job_id, 'job_id')}"
                f"/challenges/{self._identifier(challenge_id, 'challenge_id')}"
                "/viewer/frame"
            ),
        )
        self._raise_for_status(response)
        content = bytes(response.content or b"")
        content_type = str(response.headers.get("content-type") or "").lower()
        if "image/png" not in content_type or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise WorkerApiError(
                "取得ブラウザから有効な画面を受け取れませんでした。",
                code="VIEWER_FRAME_INVALID",
                status_code=response.status_code,
            )
        return content

    def send_viewer_input(
        self,
        *,
        job_id: str,
        challenge_id: str,
        action: str,
        x: int | None = None,
        y: int | None = None,
        text: str = "",
        key: str = "",
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"click", "text", "key"}:
            raise ValueError("ブラウザ操作が不正です。")
        body: dict[str, Any] = {"action": normalized_action}
        if normalized_action == "click":
            body.update({"x": int(x) if x is not None else None, "y": int(y) if y is not None else None})
        elif normalized_action == "text":
            value = str(text or "")
            if not value or len(value) > 512:
                raise ValueError("送信する文字列が不正です。")
            body["text"] = value
        else:
            body["key"] = str(key or "")
        try:
            return self._request(
                "POST",
                (
                    f"/v1/jobs/{self._identifier(job_id, 'job_id')}"
                    f"/challenges/{self._identifier(challenge_id, 'challenge_id')}"
                    "/viewer/input"
                ),
                json_body=body,
            )
        finally:
            if "text" in body:
                body["text"] = ""
            text = ""

    def complete_interactive_challenge(
        self,
        *,
        job_id: str,
        challenge_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            (
                f"/v1/jobs/{self._identifier(job_id, 'job_id')}"
                f"/challenges/{self._identifier(challenge_id, 'challenge_id')}"
                "/viewer/complete"
            ),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        response = self._perform_request(
            method,
            path,
            json_body=json_body,
            query=query,
        )

        if allow_not_found and response.status_code == 404:
            return {}

        payload: dict[str, Any] = {}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                payload = decoded
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            self._raise_for_status(response, payload=payload)

        if not payload:
            raise WorkerApiError(
                "取得ワーカーから有効な応答を受け取れませんでした。",
                code="WORKER_INVALID_RESPONSE",
                status_code=response.status_code,
            )
        return payload

    def _perform_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "",
        timeout_seconds: float | None = None,
    ) -> requests.Response:
        if json_body is not None and raw_body is not None:
            raise ValueError("JSON body and raw body are mutually exclusive")
        url = f"{self.connection.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.connection.api_token}",
            "X-GetReceipt-Owner": self.connection.owner_id,
            "Accept": "application/json, image/png",
        }
        if raw_body is not None:
            headers["Content-Type"] = str(content_type or "application/octet-stream")
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "params": query,
            "json": json_body,
            "timeout": (
                float(timeout_seconds)
                if timeout_seconds is not None
                else self.timeout_seconds
            ),
        }
        if raw_body is not None:
            request_kwargs["data"] = raw_body
        try:
            return self.session.request(
                method,
                url,
                **request_kwargs,
            )
        except requests.RequestException as error:
            raise WorkerApiError(
                "取得ワーカーへ接続できませんでした。",
                code="WORKER_UNREACHABLE",
            ) from error

    @staticmethod
    def _raise_for_status(
        response: requests.Response,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if response.status_code < 400:
            return
        decoded = payload
        if decoded is None:
            try:
                candidate = response.json()
                decoded = candidate if isinstance(candidate, dict) else {}
            except ValueError:
                decoded = {}
        public_error = decoded.get("error") if isinstance(decoded, dict) else None
        if isinstance(public_error, dict):
            message = str(public_error.get("message") or "取得ワーカーが要求を拒否しました。")
            code = str(public_error.get("code") or "WORKER_API_ERROR")
        else:
            message = "取得ワーカーが要求を拒否しました。"
            code = "WORKER_API_ERROR"
        raise WorkerApiError(message, code=code, status_code=response.status_code)

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_IDENTIFIER.fullmatch(normalized):
            raise ValueError(f"{label}の形式が不正です。")
        return normalized

    @staticmethod
    def _target_month(value: str) -> str:
        normalized = str(value or "").strip()
        if not _SAFE_MONTH.fullmatch(normalized):
            raise ValueError("target_monthの形式が不正です。")
        return normalized

    @staticmethod
    def _service_ids(values: list[str] | tuple[str, ...]) -> list[str]:
        normalized = [str(value or "").strip() for value in values]
        if not normalized or any(not _SAFE_IDENTIFIER.fullmatch(value) for value in normalized):
            raise ValueError("service_idsの形式が不正です。")
        if len(normalized) != len(set(normalized)):
            raise ValueError("service_idsに重複があります。")
        return normalized


def bearer_token_matches(provided_header: str, expected_token: str) -> bool:
    """Constant-time bearer validation shared by the deployable worker API."""

    prefix = "Bearer "
    provided = str(provided_header or "")
    if not provided.startswith(prefix):
        return False
    candidate = provided[len(prefix) :]
    expected = str(expected_token or "")
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())
