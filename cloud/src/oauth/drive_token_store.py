from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .microsoft import (
    MICROSOFT_SCOPES,
    MicrosoftOAuthError,
    _OAUTH_VALUE,
    _state_hash,
)


__all__ = ["MICROSOFT_SCOPES", "DriveMicrosoftTokenStore"]

_STORE_FILE_NAME = ".getreceipt-microsoft-oauth"
_STORE_MIME_TYPE = "application/octet-stream"


class DriveMicrosoftTokenStore:
    """Persist the Microsoft refresh token inside Google Drive.

    Streamlit Community Cloud has no durable local disk, so the encrypted
    OAuth material is stored as a single hidden file in the same Drive folder
    that already holds the receipts. The blob is Fernet-encrypted before it
    ever leaves the process; Drive only sees ciphertext.
    """

    def __init__(
        self,
        *,
        drive_service: Any,
        folder_id: str,
        encryption_key: str,
    ) -> None:
        self._service = drive_service
        self._folder_id = str(folder_id)
        self._fernet = Fernet(str(encryption_key).encode("ascii"))
        self._file_id_cache = ""

    # -- public token-store interface (matches MicrosoftTokenStore) --------

    def connected(self) -> bool:
        blob = self._load_blob()
        return len(str(blob.get("refresh_token") or "")) >= 20

    def updated_at(self) -> str:
        return str(self._load_blob().get("updated_at") or "")

    def save_refresh_token(self, refresh_token: str) -> None:
        value = str(refresh_token or "")
        if len(value) < 20:
            raise MicrosoftOAuthError(
                "Microsoftの長期認証情報を確認できませんでした。",
                code="MICROSOFT_REFRESH_TOKEN_MISSING",
            )
        blob = self._load_blob()
        blob["refresh_token"] = value
        blob["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_blob(blob)
        value = ""

    def load_refresh_token(self) -> str:
        blob = self._load_blob()
        token = str(blob.get("refresh_token") or "")
        if len(token) < 20:
            raise MicrosoftOAuthError(
                "Microsoftメールが未接続です。",
                code="MICROSOFT_OAUTH_REQUIRED",
            )
        return token

    def save_pending_authorization(
        self,
        *,
        state: str,
        code_verifier: str,
        expires_at: datetime,
    ) -> None:
        normalized_state = str(state or "").strip()
        normalized_verifier = str(code_verifier or "").strip()
        if (
            not _OAUTH_VALUE.fullmatch(normalized_state)
            or not _OAUTH_VALUE.fullmatch(normalized_verifier)
        ):
            raise ValueError("OAuth pending authorizationの形式が不正です。")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        blob = self._load_blob()
        pending = self._purge_expired(blob.get("pending"))
        pending[_state_hash(normalized_state)] = {
            "verifier": normalized_verifier,
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        }
        blob["pending"] = pending
        self._save_blob(blob)

    def consume_pending_authorization(self, *, state: str) -> str | None:
        normalized_state = str(state or "").strip()
        if not _OAUTH_VALUE.fullmatch(normalized_state):
            return None
        blob = self._load_blob()
        pending = dict(blob.get("pending") or {})
        entry = pending.pop(_state_hash(normalized_state), None)
        blob["pending"] = self._purge_expired(pending)
        self._save_blob(blob)
        if not isinstance(entry, dict):
            return None
        if self._expired(str(entry.get("expires_at") or "")):
            return None
        verifier = str(entry.get("verifier") or "")
        return verifier if _OAUTH_VALUE.fullmatch(verifier) else None

    def delete(self) -> None:
        self._save_blob({"refresh_token": "", "pending": {}, "updated_at": ""})

    # -- Drive persistence helpers -----------------------------------------

    def _load_blob(self) -> dict[str, Any]:
        file_id = self._find_file_id()
        if not file_id:
            return {}
        try:
            raw = self._service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            ).execute()
        except Exception:
            return {}
        if not isinstance(raw, (bytes, bytearray)):
            return {}
        try:
            decrypted = self._fernet.decrypt(bytes(raw))
            value = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_blob(self, blob: dict[str, Any]) -> None:
        payload = self._fernet.encrypt(
            json.dumps(blob, ensure_ascii=False).encode("utf-8")
        )
        media = self._media_upload(payload)
        file_id = self._find_file_id()
        if file_id:
            self._service.files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
            return
        created = self._service.files().create(
            body={"name": _STORE_FILE_NAME, "parents": [self._folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        self._file_id_cache = str(created.get("id") or "")

    def _find_file_id(self) -> str:
        if self._file_id_cache:
            return self._file_id_cache
        query = (
            f"name = '{_STORE_FILE_NAME}' and "
            f"'{self._folder_id}' in parents and trashed = false"
        )
        result = self._service.files().list(
            q=query,
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        self._file_id_cache = str(files[0].get("id") or "") if files else ""
        return self._file_id_cache

    @staticmethod
    def _media_upload(content: bytes):
        from io import BytesIO

        from googleapiclient.http import MediaIoBaseUpload

        return MediaIoBaseUpload(
            BytesIO(content),
            mimetype=_STORE_MIME_TYPE,
            resumable=False,
        )

    def _purge_expired(self, pending: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if isinstance(pending, dict):
            for key, entry in pending.items():
                if isinstance(entry, dict) and not self._expired(
                    str(entry.get("expires_at") or "")
                ):
                    result[str(key)] = entry
        return result

    @staticmethod
    def _expired(expires_at: str) -> bool:
        if not expires_at:
            return True
        try:
            deadline = datetime.fromisoformat(expires_at)
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= deadline
