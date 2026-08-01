from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.storage.status_store import (  # noqa: E402
    MAX_MONTHS,
    STATUS_FILE_NAME,
    ServiceStatusStore,
)


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, blobs, *, writable=True):
        self.blobs = blobs
        self.writable = writable
        self.writes = 0

    def list(self, **kwargs):
        found = [{"id": "status-1"}] if STATUS_FILE_NAME in self.blobs else []
        return _Execute({"files": found})

    def get_media(self, **kwargs):
        return _Execute(self.blobs[STATUS_FILE_NAME])

    def create(self, **kwargs):
        if not self.writable:
            raise RuntimeError("read only")
        self.writes += 1
        self.blobs[STATUS_FILE_NAME] = kwargs["media_body"]._fd.getvalue()
        return _Execute({"id": "status-1"})

    def update(self, **kwargs):
        if not self.writable:
            raise RuntimeError("read only")
        self.writes += 1
        self.blobs[STATUS_FILE_NAME] = kwargs["media_body"]._fd.getvalue()
        return _Execute({"id": "status-1"})


class _Service:
    def __init__(self, blobs, *, writable=True):
        self._files = _Files(blobs, writable=writable)

    def files(self):
        return self._files


class ServiceStatusStoreTest(unittest.TestCase):
    """A status the owner saw must still be there when they come back."""

    def setUp(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.store = ServiceStatusStore(_Service(self.blobs), "folder-1")

    def test_an_outcome_survives_a_new_session(self) -> None:
        self.store.record(
            target_month="2026-07",
            service_id="commufa",
            code="COMMUFA_MONTH_NOT_ISSUED",
            message="まだ掲載されていません。",
            detail="請求が確定してから再実行してください。",
        )

        # A brand-new store, as a page reload would build.
        fresh = ServiceStatusStore(_Service(self.blobs), "folder-1")
        entry = fresh.load()["2026-07"]["commufa"]

        self.assertEqual("COMMUFA_MONTH_NOT_ISSUED", entry["code"])
        self.assertEqual("まだ掲載されていません。", entry["message"])
        self.assertIn("at", entry)

    def test_each_service_and_month_is_kept_apart(self) -> None:
        self.store.record(
            target_month="2026-07", service_id="commufa", code="A", message="a"
        )
        self.store.record(
            target_month="2026-07", service_id="mobile", code="B", message="b"
        )
        self.store.record(
            target_month="2026-06", service_id="commufa", code="C", message="c"
        )

        months = self.store.load()

        self.assertEqual({"commufa", "mobile"}, set(months["2026-07"]))
        self.assertEqual("C", months["2026-06"]["commufa"]["code"])

    def test_a_saved_month_drops_its_remembered_reason(self) -> None:
        self.store.record(
            target_month="2026-07", service_id="commufa", code="A", message="a"
        )

        self.store.clear(target_month="2026-07", service_id="commufa")

        self.assertEqual({}, self.store.load())

    def test_clearing_something_never_recorded_is_harmless(self) -> None:
        self.assertTrue(self.store.clear(target_month="2026-07", service_id="epos"))

    def test_the_file_does_not_grow_without_limit(self) -> None:
        for index in range(MAX_MONTHS + 6):
            self.store.record(
                target_month=f"20{20 + index // 12:02d}-{index % 12 + 1:02d}",
                service_id="commufa",
                code="A",
                message="a",
            )

        months = self.store.load()

        self.assertEqual(MAX_MONTHS, len(months))
        # The newest are the ones worth keeping; the oldest fall off.
        recorded = sorted(
            f"20{20 + index // 12:02d}-{index % 12 + 1:02d}"
            for index in range(MAX_MONTHS + 6)
        )
        self.assertEqual(set(recorded[-MAX_MONTHS:]), set(months))
        payload = json.loads(self.blobs[STATUS_FILE_NAME].decode("utf-8"))
        self.assertEqual(MAX_MONTHS, len(payload["months"]))

    def test_a_drive_that_refuses_the_write_is_not_fatal(self) -> None:
        store = ServiceStatusStore(_Service({}, writable=False), "folder-1")

        self.assertFalse(
            store.record(target_month="2026-07", service_id="commufa", code="A", message="a")
        )

    def test_an_unreadable_file_reads_as_empty(self) -> None:
        self.blobs[STATUS_FILE_NAME] = b"not json"

        self.assertEqual({}, self.store.load())


if __name__ == "__main__":
    unittest.main()
