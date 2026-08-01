"""Remember what happened to each month, past the browser session.

A month's outcome - saved, not billed yet, or failed and why - only ever
lived in Streamlit's session state, so reloading the page threw it away. The
owner came back to a screen that had forgotten the run they just watched,
which makes the status meaningless: it can only ever describe the last few
minutes.

Drive already holds the receipts and survives everything, so the outcomes go
there too, as one small file beside them. It is written only when something
changes, and a Drive that cannot be reached simply means the app behaves as
it did before.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any


__all__ = ["STATUS_FILE_NAME", "ServiceStatusStore"]


LOGGER = logging.getLogger(__name__)

STATUS_FILE_NAME = ".getreceipt-status.json"
_MIME_TYPE = "application/json"
# One entry per service per month. Older months are dropped so the file stays
# small no matter how long the app is used.
MAX_MONTHS = 36


class ServiceStatusStore:
    """Persist each service-month outcome next to the receipts."""

    def __init__(self, service: Any, folder_id: str) -> None:
        self._service = service
        self._folder_id = str(folder_id)
        self._file_id = ""

    def load(self) -> dict[str, dict[str, dict[str, str]]]:
        """Return {month: {service_id: outcome}}; empty when unavailable."""

        try:
            file_id = self._find()
            if not file_id:
                return {}
            raw = self._service.files().get_media(
                fileId=file_id, supportsAllDrives=True
            ).execute()
            payload = json.loads(bytes(raw).decode("utf-8"))
        except Exception:
            LOGGER.info("Stored month outcomes could not be read")
            return {}
        if not isinstance(payload, dict):
            return {}
        months = payload.get("months")
        return months if isinstance(months, dict) else {}

    def record(
        self,
        *,
        target_month: str,
        service_id: str,
        code: str,
        message: str,
        detail: str = "",
    ) -> bool:
        """Remember one outcome. Returns False when it could not be kept."""

        months = self.load()
        entries = dict(months.get(target_month) or {})
        entries[service_id] = {
            "code": str(code),
            "message": str(message),
            "detail": str(detail),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        months[target_month] = entries
        return self._save(months)

    def clear(self, *, target_month: str, service_id: str) -> bool:
        """Drop one outcome, because that month has since been saved."""

        months = self.load()
        entries = dict(months.get(target_month) or {})
        if service_id not in entries:
            return True
        entries.pop(service_id, None)
        if entries:
            months[target_month] = entries
        else:
            months.pop(target_month, None)
        return self._save(months)

    # -- Drive persistence --------------------------------------------------

    def _save(self, months: dict[str, Any]) -> bool:
        trimmed = {
            month: months[month]
            for month in sorted(months, reverse=True)[:MAX_MONTHS]
        }
        payload = json.dumps(
            {"months": trimmed}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        try:
            media = _media(payload)
            file_id = self._find()
            if file_id:
                self._service.files().update(
                    fileId=file_id, media_body=media, supportsAllDrives=True
                ).execute()
                return True
            created = self._service.files().create(
                body={"name": STATUS_FILE_NAME, "parents": [self._folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            self._file_id = str(created.get("id") or "")
        except Exception:
            # Losing the record is a smaller harm than losing the acquisition.
            LOGGER.info("Month outcome could not be stored")
            return False
        return True

    def _find(self) -> str:
        if self._file_id:
            return self._file_id
        query = (
            f"name = '{STATUS_FILE_NAME}' and "
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
        self._file_id = str(files[0].get("id") or "") if files else ""
        return self._file_id


def _media(payload: bytes):
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(BytesIO(payload), mimetype=_MIME_TYPE, resumable=False)
