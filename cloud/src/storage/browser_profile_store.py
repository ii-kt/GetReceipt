"""Keep each provider's browser recognisable between acquisitions.

Every acquisition used to start from an empty Chrome profile and delete it
afterwards, so each provider saw a brand-new, never-seen-before browser every
single time. That is exactly the shape risk checks react to: Epos answers it
with an image puzzle, and the others mail a fresh verification code.

Streamlit Community Cloud has no durable disk, so the small part of the
profile that makes a browser recognisable - its cookies and site storage - is
archived, encrypted, and kept in the same Google Drive folder as the receipts.
Drive only ever sees ciphertext.
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


__all__ = ["BrowserProfileStore", "PROFILE_MEMBER_PREFIXES"]


LOGGER = logging.getLogger(__name__)

_FILE_PREFIX = ".getreceipt-profile-"
_MIME_TYPE = "application/octet-stream"
# A session lives in the cookie jar and site storage. Everything else in a
# Chrome profile is cache and machine state, which must not travel.
PROFILE_MEMBER_PREFIXES = (
    "Default/Cookies",
    "Default/Network/Cookies",
    "Default/Local Storage/",
    "Default/Preferences",
)
# Generous for cookies and site storage, small enough that a runaway profile
# can never turn into a large Drive upload.
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
_SAFE_SERVICE_ID = "abcdefghijklmnopqrstuvwxyz0123456789-_"


class BrowserProfileStore:
    """Archive and restore the recognisable part of a browser profile."""

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
        self._file_ids: dict[str, str] = {}

    def restore(self, service_id: str, profile_dir: Path) -> bool:
        """Lay a saved profile into ``profile_dir``. False when there is none.

        A missing or unreadable archive is never fatal: the acquisition simply
        starts from a fresh profile, which is what it did before.
        """

        name = self._file_name(service_id)
        try:
            payload = self._download(name)
            if not payload:
                return False
            archive = self._fernet.decrypt(payload)
        except InvalidToken:
            LOGGER.info("Stored browser profile could not be decrypted; ignoring it")
            return False
        except Exception:
            LOGGER.info("Stored browser profile could not be read; ignoring it")
            return False
        try:
            root = Path(profile_dir).resolve()
            root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                for member in bundle.getmembers():
                    if not _is_safe_member(member, root):
                        continue
                    bundle.extract(member, path=root, filter="data")
        except Exception:
            LOGGER.info("Stored browser profile could not be unpacked; ignoring it")
            return False
        return True

    def save(self, service_id: str, profile_dir: Path) -> bool:
        """Archive the profile. Call only once the browser has been closed.

        Chrome holds its cookie database open, so a copy taken while it is
        running can be torn. Returns False when there is nothing worth saving.
        """

        root = Path(profile_dir)
        if not root.is_dir():
            return False
        buffer = io.BytesIO()
        included = 0
        try:
            with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(root).as_posix()
                    if not relative.startswith(PROFILE_MEMBER_PREFIXES):
                        continue
                    bundle.add(path, arcname=relative)
                    included += 1
        except Exception:
            LOGGER.info("Browser profile could not be archived")
            return False
        if not included:
            return False
        payload = buffer.getvalue()
        if len(payload) > MAX_ARCHIVE_BYTES:
            LOGGER.info("Browser profile archive is too large to keep")
            return False
        try:
            self._upload(self._file_name(service_id), self._fernet.encrypt(payload))
        except Exception:
            LOGGER.info("Browser profile could not be stored")
            return False
        return True

    def forget(self, service_id: str) -> bool:
        """Drop the saved profile, so the next run signs in from scratch."""

        name = self._file_name(service_id)
        try:
            file_id = self._find_file_id(name)
            if not file_id:
                return False
            self._service.files().delete(
                fileId=file_id, supportsAllDrives=True
            ).execute()
        except Exception:
            return False
        self._file_ids.pop(name, None)
        return True

    # -- Drive persistence helpers -----------------------------------------

    @staticmethod
    def _file_name(service_id: str) -> str:
        normalized = str(service_id or "").strip().lower()
        if not normalized or any(ch not in _SAFE_SERVICE_ID for ch in normalized):
            raise ValueError("invalid service_id")
        return f"{_FILE_PREFIX}{normalized}"

    def _download(self, name: str) -> bytes:
        file_id = self._find_file_id(name)
        if not file_id:
            return b""
        raw = self._service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        ).execute()
        return bytes(raw) if isinstance(raw, (bytes, bytearray)) else b""

    def _upload(self, name: str, payload: bytes) -> None:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype=_MIME_TYPE, resumable=False
        )
        file_id = self._find_file_id(name)
        if file_id:
            self._service.files().update(
                fileId=file_id, media_body=media, supportsAllDrives=True
            ).execute()
            return
        created = self._service.files().create(
            body={"name": name, "parents": [self._folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        self._file_ids[name] = str(created.get("id") or "")

    def _find_file_id(self, name: str) -> str:
        cached = self._file_ids.get(name)
        if cached:
            return cached
        query = f"name = '{name}' and '{self._folder_id}' in parents and trashed = false"
        result = self._service.files().list(
            q=query,
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = result.get("files", [])
        file_id = str(files[0].get("id") or "") if files else ""
        if file_id:
            self._file_ids[name] = file_id
        return file_id


def _is_safe_member(member: tarfile.TarInfo, root: Path) -> bool:
    """Accept only plain files that stay inside the profile directory."""

    if not member.isfile():
        return False
    name = member.name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return False
    if not name.startswith(PROFILE_MEMBER_PREFIXES):
        return False
    try:
        destination = (root / name).resolve()
    except OSError:
        return False
    return destination == root or root in destination.parents
