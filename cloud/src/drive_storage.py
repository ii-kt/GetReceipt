from __future__ import annotations

import json
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from .config import RECEIPT_DRIVE_FOLDER_ID


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class DriveConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DriveUploadResult:
    id: str
    name: str
    web_view_link: str


def _normalize_private_key(info: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


def load_service_account_info(secrets: Any | None = None) -> dict[str, Any]:
    if secrets is not None and "google_service_account" in secrets:
        return _normalize_private_key(dict(secrets["google_service_account"]))

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if raw_json:
        return _normalize_private_key(json.loads(raw_json))

    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials_path:
        with open(credentials_path, "r", encoding="utf-8") as file:
            return _normalize_private_key(json.load(file))

    raise DriveConfigError(
        "\u30b5\u30fc\u30d3\u30b9\u30a2\u30ab\u30a6\u30f3\u30c8\u304c\u672a\u8a2d\u5b9a\u3067\u3059\u3002"
        "Streamlit Secrets\u306e google_service_account \u306bJSON\u3092\u8a2d\u5b9a\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
    )


def build_drive_service(service_account_info: dict[str, Any]):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ModuleNotFoundError as error:
        raise DriveConfigError(
            "Google Drive\u9023\u643a\u30e9\u30a4\u30d6\u30e9\u30ea\u304c\u4e0d\u8db3\u3057\u3066\u3044\u307e\u3059\u3002"
            "requirements.txt\u3092\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
        ) from error

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[DRIVE_SCOPE],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class DriveStorage:
    def __init__(self, service: Any, folder_id: str = RECEIPT_DRIVE_FOLDER_ID):
        self.service = service
        self.folder_id = folder_id

    @classmethod
    def from_secrets(cls, secrets: Any | None = None, folder_id: str = RECEIPT_DRIVE_FOLDER_ID) -> "DriveStorage":
        info = load_service_account_info(secrets)
        return cls(build_drive_service(info), folder_id=folder_id)

    def upload_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> DriveUploadResult:
        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ModuleNotFoundError as error:
            raise DriveConfigError(
                "Google Drive\u9023\u643a\u30e9\u30a4\u30d6\u30e9\u30ea\u304c\u4e0d\u8db3\u3057\u3066\u3044\u307e\u3059\u3002"
                "requirements.txt\u3092\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
            ) from error

        media = MediaIoBaseUpload(BytesIO(content), mimetype=mime_type, resumable=False)
        metadata = {
            "name": file_name,
            "parents": [self.folder_id],
        }
        created = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return DriveUploadResult(
            id=created.get("id", ""),
            name=created.get("name", file_name),
            web_view_link=created.get("webViewLink", ""),
        )

    def upsert_bytes(self, *, file_name: str, content: bytes, mime_type: str) -> DriveUploadResult:
        existing = self._find_first_by_name(file_name)
        if not existing:
            return self.upload_bytes(file_name=file_name, content=content, mime_type=mime_type)

        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ModuleNotFoundError as error:
            raise DriveConfigError(
                "Google Drive\u9023\u643a\u30e9\u30a4\u30d6\u30e9\u30ea\u304c\u4e0d\u8db3\u3057\u3066\u3044\u307e\u3059\u3002"
                "requirements.txt\u3092\u30a4\u30f3\u30b9\u30c8\u30fc\u30eb\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
            ) from error

        media = MediaIoBaseUpload(BytesIO(content), mimetype=mime_type, resumable=False)
        updated = self.service.files().update(
            fileId=existing["id"],
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return DriveUploadResult(
            id=updated.get("id", existing["id"]),
            name=updated.get("name", file_name),
            web_view_link=updated.get("webViewLink", existing.get("webViewLink", "")),
        )

    def list_files(self) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        page_token = None
        query = f"'{self.folder_id}' in parents and trashed = false"
        while True:
            result = self.service.files().list(
                q=query,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)",
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

    def _find_first_by_name(self, file_name: str) -> dict[str, str] | None:
        escaped_name = file_name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped_name}' and '{self.folder_id}' in parents and trashed = false"
        result = self.service.files().list(
            q=query,
            fields="files(id,name,webViewLink)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        return files[0] if files else None

