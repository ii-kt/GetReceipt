from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from ..config import RECEIPT_DRIVE_FOLDER_ID


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class DriveConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveUploadResult:
    id: str
    name: str
    web_view_link: str


_RECEIPT_KEY_PROPERTY = "getreceiptKey"
_CONTENT_HASH_PROPERTY = "getreceiptSha256"


def _normalize_private_key(info: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


def load_service_account_info(secrets: Any | None = None) -> dict[str, Any]:
    if secrets is not None and "google_service_account" in secrets:
        return _normalize_private_key(dict(secrets["google_service_account"]))

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON"
    )
    if raw_json:
        return _normalize_private_key(json.loads(raw_json))

    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials_path:
        with open(credentials_path, "r", encoding="utf-8") as file:
            return _normalize_private_key(json.load(file))

    raise DriveConfigError(
        "サービスアカウントが未設定です。"
        "Streamlit Secretsの google_service_account にJSONを設定してください。"
    )


def build_drive_service(service_account_info: dict[str, Any]):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ModuleNotFoundError as error:
        raise DriveConfigError("Google Drive連携ライブラリが不足しています。") from error

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[DRIVE_SCOPE],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class DriveStorage:
    """The narrow Drive adapter required by the automatic acquisition app."""

    def __init__(self, service: Any, folder_id: str = RECEIPT_DRIVE_FOLDER_ID):
        self.service = service
        self.folder_id = folder_id

    @classmethod
    def from_secrets(
        cls,
        secrets: Any | None = None,
        folder_id: str = RECEIPT_DRIVE_FOLDER_ID,
    ) -> "DriveStorage":
        info = load_service_account_info(secrets)
        return cls(build_drive_service(info), folder_id=folder_id)

    def upload_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> DriveUploadResult:
        properties = self._receipt_properties(file_name, content)
        media_upload = self._media_upload(content, mime_type)
        created = self.service.files().create(
            body={
                "name": file_name,
                "parents": [self.folder_id],
                "appProperties": properties,
            },
            media_body=media_upload,
            fields="id,name,webViewLink,size,appProperties,md5Checksum",
            supportsAllDrives=True,
        ).execute()
        verified = self._verify_uploaded_file(
            file_id=str(created.get("id") or ""),
            expected_name=file_name,
            expected_size=len(content),
            expected_hash=properties[_CONTENT_HASH_PROPERTY],
            expected_md5=hashlib.md5(content, usedforsecurity=False).hexdigest(),
        )
        return self._upload_result(verified, fallback=created, file_name=file_name)

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> DriveUploadResult:
        properties = self._receipt_properties(file_name, content)
        existing = self._find_first_by_receipt_key(
            properties[_RECEIPT_KEY_PROPERTY],
        ) or self._find_first_by_name(file_name)
        if existing is None:
            return self.upload_bytes(file_name=file_name, content=content, mime_type=mime_type)

        existing_properties = existing.get("appProperties") or {}
        content_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        if (
            existing.get("name") == file_name
            and existing_properties.get(_CONTENT_HASH_PROPERTY)
            == properties[_CONTENT_HASH_PROPERTY]
            and str(existing.get("size") or "") == str(len(content))
            and str(existing.get("md5Checksum") or "").lower() == content_md5
        ):
            return self._upload_result(existing, file_name=file_name)

        updated = self.service.files().update(
            fileId=existing["id"],
            body={
                "name": file_name,
                "appProperties": properties,
            },
            media_body=self._media_upload(content, mime_type),
            fields="id,name,webViewLink,size,appProperties,md5Checksum",
            supportsAllDrives=True,
        ).execute()
        verified = self._verify_uploaded_file(
            file_id=str(updated.get("id") or existing["id"]),
            expected_name=file_name,
            expected_size=len(content),
            expected_hash=properties[_CONTENT_HASH_PROPERTY],
            expected_md5=content_md5,
        )
        return self._upload_result(
            verified,
            fallback={**existing, **updated},
            file_name=file_name,
        )

    def list_files(self) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        page_token = None
        query = f"'{self.folder_id}' in parents and trashed = false"
        while True:
            result = self.service.files().list(
                q=query,
                fields=(
                    "nextPageToken,"
                    "files(id,name,mimeType,size,modifiedTime,webViewLink,"
                    "appProperties,md5Checksum)"
                ),
                pageSize=1000,
                pageToken=page_token,
                orderBy="name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return files

    @staticmethod
    def _media_upload(content: bytes, mime_type: str):
        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ModuleNotFoundError as error:
            raise DriveConfigError("Google Drive連携ライブラリが不足しています。") from error
        return MediaIoBaseUpload(BytesIO(content), mimetype=mime_type, resumable=False)

    def _find_first_by_name(self, file_name: str) -> dict[str, str] | None:
        escaped_name = file_name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped_name}' and '{self.folder_id}' in parents and trashed = false"
        result = self.service.files().list(
            q=query,
            fields="files(id,name,webViewLink,size,appProperties,md5Checksum)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        return files[0] if files else None

    def _find_first_by_receipt_key(self, receipt_key: str) -> dict[str, Any] | None:
        escaped_key = receipt_key.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"appProperties has {{ key='{_RECEIPT_KEY_PROPERTY}' "
            f"and value='{escaped_key}' }} and "
            f"'{self.folder_id}' in parents and trashed = false"
        )
        result = self.service.files().list(
            q=query,
            fields="files(id,name,webViewLink,size,appProperties,md5Checksum)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        return files[0] if files else None

    def _verify_uploaded_file(
        self,
        *,
        file_id: str,
        expected_name: str,
        expected_size: int,
        expected_hash: str,
        expected_md5: str,
    ) -> dict[str, Any]:
        if not file_id:
            raise RuntimeError("Google Drive did not return a file ID.")
        stored = self.service.files().get(
            fileId=file_id,
            fields="id,name,webViewLink,size,appProperties,md5Checksum",
            supportsAllDrives=True,
        ).execute()
        properties = stored.get("appProperties") or {}
        if str(stored.get("name") or "") != expected_name:
            raise RuntimeError("Google Drive returned a different file name.")
        if str(stored.get("size") or "") != str(expected_size):
            raise RuntimeError("Google Drive returned a different file size.")
        if properties.get(_CONTENT_HASH_PROPERTY) != expected_hash:
            raise RuntimeError("Google Drive content verification failed.")
        if str(stored.get("md5Checksum") or "").lower() != expected_md5:
            raise RuntimeError("Google Drive stored content checksum mismatch.")
        return stored

    def _receipt_properties(self, file_name: str, content: bytes) -> dict[str, str]:
        receipt_material = f"{self.folder_id}\0{file_name}".encode("utf-8")
        return {
            _RECEIPT_KEY_PROPERTY: hashlib.sha256(receipt_material).hexdigest(),
            _CONTENT_HASH_PROPERTY: hashlib.sha256(content).hexdigest(),
        }

    @staticmethod
    def _upload_result(
        primary: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
        file_name: str,
    ) -> DriveUploadResult:
        fallback = fallback or {}
        return DriveUploadResult(
            id=str(primary.get("id") or fallback.get("id") or ""),
            name=str(primary.get("name") or fallback.get("name") or file_name),
            web_view_link=str(
                primary.get("webViewLink") or fallback.get("webViewLink") or ""
            ),
        )
