from __future__ import annotations

import sys
import unittest
import hashlib
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.storage.drive_storage import DriveStorage  # noqa: E402


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return deepcopy(self.result)


class _Files:
    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}
        self.corrupt_checksum_on_get = False
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.list_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        body = deepcopy(kwargs["body"])
        file_id = f"file-{len(self.create_calls)}"
        item = {
            "id": file_id,
            "name": body["name"],
            "webViewLink": f"https://drive.example/{file_id}",
            "size": str(len(kwargs["media_body"])),
            "md5Checksum": hashlib.md5(
                kwargs["media_body"],
                usedforsecurity=False,
            ).hexdigest(),
            "appProperties": body["appProperties"],
        }
        self.stored[file_id] = item
        return _Request(item)

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        item = self.stored[kwargs["fileId"]]
        item.update(deepcopy(kwargs["body"]))
        item["size"] = str(len(kwargs["media_body"]))
        item["md5Checksum"] = hashlib.md5(
            kwargs["media_body"],
            usedforsecurity=False,
        ).hexdigest()
        return _Request(item)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        item = deepcopy(self.stored[kwargs["fileId"]])
        if self.corrupt_checksum_on_get:
            item["md5Checksum"] = "0" * 32
        return _Request(item)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        query = kwargs["q"]
        matches = []
        for item in self.stored.values():
            if "appProperties has" in query:
                if item.get("appProperties", {}).get("getreceiptKey") in query:
                    matches.append(item)
            elif f"name = '{item['name']}'" in query:
                matches.append(item)
        return _Request({"files": matches[:1]})


class _Drive:
    def __init__(self) -> None:
        self.files_api = _Files()

    def files(self):
        return self.files_api


class DriveStorageIdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.drive = _Drive()
        self.storage = DriveStorage(self.drive, folder_id="folder-1")
        self.media = patch.object(
            DriveStorage,
            "_media_upload",
            side_effect=lambda content, _mime: content,
        )
        self.media.start()
        self.addCleanup(self.media.stop)

    def test_create_records_hash_and_verifies_by_file_id(self) -> None:
        result = self.storage.upsert_bytes(
            file_name="receipt.pdf",
            content=b"%PDF first",
            mime_type="application/pdf",
        )

        self.assertEqual("file-1", result.id)
        self.assertEqual(1, len(self.drive.files_api.create_calls))
        body = self.drive.files_api.create_calls[0]["body"]
        self.assertEqual({"receipt.pdf"}, {body["name"]})
        self.assertEqual(
            {"getreceiptKey", "getreceiptSha256"},
            set(body["appProperties"]),
        )
        self.assertEqual(1, len(self.drive.files_api.get_calls))

    def test_retry_with_same_content_does_not_upload_again(self) -> None:
        first = self.storage.upsert_bytes(
            file_name="receipt.pdf",
            content=b"%PDF same",
            mime_type="application/pdf",
        )
        second = self.storage.upsert_bytes(
            file_name="receipt.pdf",
            content=b"%PDF same",
            mime_type="application/pdf",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.drive.files_api.create_calls))
        self.assertEqual([], self.drive.files_api.update_calls)

    def test_changed_content_updates_and_reverifies_same_file(self) -> None:
        first = self.storage.upsert_bytes(
            file_name="receipt.pdf",
            content=b"%PDF old",
            mime_type="application/pdf",
        )
        second = self.storage.upsert_bytes(
            file_name="receipt.pdf",
            content=b"%PDF new and longer",
            mime_type="application/pdf",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.drive.files_api.create_calls))
        self.assertEqual(1, len(self.drive.files_api.update_calls))
        self.assertEqual(2, len(self.drive.files_api.get_calls))

    def test_drive_reported_content_checksum_mismatch_fails_closed(self) -> None:
        self.drive.files_api.corrupt_checksum_on_get = True

        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.storage.upsert_bytes(
                file_name="receipt.pdf",
                content=b"%PDF expected",
                mime_type="application/pdf",
            )


if __name__ == "__main__":
    unittest.main()
