"""Keep a working Drive credential where the app can always reach it.

The owner's Google refresh token lives in Streamlit secrets, and secrets can
only be edited by hand. When that one value stops working the app loses the
receipts folder, and with it every other credential it keeps there - so it
cannot even write itself a replacement. Recovery meant editing secrets on a
phone, every time.

There is one credential in secrets that never expires: the service account
key. A service account cannot create files in a consumer Drive, but it can
read a file the owner has shared with it. So a reconnected refresh token is
written to the receipts folder as the owner, shared with the service account,
and read back through that account the next time the secrets copy fails.

After one reconnection the secrets copy stops mattering: the app recovers on
its own. The blob is encrypted before it leaves the process, so Drive - and
the service account - only ever see ciphertext.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


__all__ = ["GoogleCredentialStore", "STORE_FILE_NAME"]


LOGGER = logging.getLogger(__name__)

STORE_FILE_NAME = ".getreceipt-google-oauth"
_MIME_TYPE = "application/octet-stream"


class GoogleCredentialStore:
    """Read and write the owner's Drive refresh token inside Drive itself."""

    def __init__(self, *, folder_id: str, encryption_key: str) -> None:
        self._folder_id = str(folder_id)
        self._fernet = Fernet(str(encryption_key).encode("ascii"))

    # -- writing (as the owner, right after a reconnection) -----------------

    def save(self, owner_service: Any, refresh_token: str, *, share_with: str = "") -> bool:
        """Store the token and let the service account read it back."""

        value = str(refresh_token or "")
        if len(value) < 20:
            return False
        payload = self._fernet.encrypt(
            json.dumps(
                {
                    "refresh_token": value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).encode("utf-8")
        )
        try:
            file_id = _find(owner_service, self._folder_id)
            media = _media(payload)
            if file_id:
                owner_service.files().update(
                    fileId=file_id, media_body=media, supportsAllDrives=True
                ).execute()
            else:
                created = owner_service.files().create(
                    body={"name": STORE_FILE_NAME, "parents": [self._folder_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                file_id = str(created.get("id") or "")
        except Exception:
            LOGGER.info("Drive credential could not be stored")
            return False
        if file_id and share_with:
            _share(owner_service, file_id, share_with)
        return bool(file_id)

    # -- reading (as the service account, when secrets have gone stale) -----

    def load(self, reader_service: Any) -> str:
        """Return the stored token, or "" when there is none to read."""

        try:
            file_id = _find(reader_service, self._folder_id)
            if not file_id:
                return ""
            raw = reader_service.files().get_media(
                fileId=file_id, supportsAllDrives=True
            ).execute()
            if not isinstance(raw, (bytes, bytearray)):
                return ""
            blob = json.loads(self._fernet.decrypt(bytes(raw)).decode("utf-8"))
        except InvalidToken:
            LOGGER.info("Stored Drive credential could not be decrypted")
            return ""
        except Exception:
            LOGGER.info("Stored Drive credential could not be read")
            return ""
        token = str(blob.get("refresh_token") or "") if isinstance(blob, dict) else ""
        return token if len(token) >= 20 else ""


def _find(service: Any, folder_id: str) -> str:
    query = (
        f"name = '{STORE_FILE_NAME}' and '{folder_id}' in parents and trashed = false"
    )
    result = service.files().list(
        q=query,
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    return str(files[0].get("id") or "") if files else ""


def _media(payload: bytes):
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(BytesIO(payload), mimetype=_MIME_TYPE, resumable=False)


def _share(service: Any, file_id: str, address: str) -> None:
    """Give the service account read access to this one file, nothing else."""

    try:
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "reader", "emailAddress": address},
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()
    except Exception:
        # Already shared, or sharing refused. Saving still succeeded; only the
        # unattended recovery path is unavailable.
        LOGGER.info("Drive credential file was not shared with the service account")
