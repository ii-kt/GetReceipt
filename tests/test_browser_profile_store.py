from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from cryptography.fernet import Fernet  # noqa: E402

from src.storage.browser_profile_store import BrowserProfileStore  # noqa: E402


KEY = Fernet.generate_key().decode("ascii")


class FakeDriveFiles:
    def __init__(self, store: dict[str, bytes]) -> None:
        self.store = store
        self.uploaded: list[str] = []

    def list(self, **kwargs):
        query = kwargs.get("q", "")
        name = query.split("name = '", 1)[1].split("'", 1)[0]
        found = [{"id": name}] if name in self.store else []
        return _Execute({"files": found})

    def get_media(self, **kwargs):
        return _Execute(self.store.get(kwargs["fileId"], b""))

    def create(self, **kwargs):
        name = kwargs["body"]["name"]
        self.store[name] = kwargs["media_body"]._fd.getvalue()
        self.uploaded.append(name)
        return _Execute({"id": name})

    def update(self, **kwargs):
        file_id = kwargs["fileId"]
        self.store[file_id] = kwargs["media_body"]._fd.getvalue()
        self.uploaded.append(file_id)
        return _Execute({"id": file_id})

    def delete(self, **kwargs):
        self.store.pop(kwargs["fileId"], None)
        return _Execute({})


class _Execute:
    def __init__(self, value) -> None:
        self.value = value

    def execute(self):
        return self.value


class FakeDriveService:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self._files = FakeDriveFiles(self.store)

    def files(self):
        return self._files


def write_profile(root: Path) -> None:
    (root / "Default" / "Network").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Local Storage" / "leveldb").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Cache" / "Cache_Data").mkdir(parents=True, exist_ok=True)
    (root / "Default" / "Cookies").write_bytes(b"SQLite format 3\x00cookie-jar")
    (root / "Default" / "Network" / "Cookies").write_bytes(b"SQLite format 3\x00net")
    (root / "Default" / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"ls")
    (root / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (root / "Default" / "Cache" / "Cache_Data" / "data_0").write_bytes(b"x" * 4096)
    (root / "Default" / "History").write_bytes(b"browsing history")


class BrowserProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeDriveService()
        self.store = BrowserProfileStore(
            drive_service=self.service, folder_id="folder-1", encryption_key=KEY
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_saved_profile_comes_back_on_the_next_run(self) -> None:
        source = self.root / "run-1" / "profile"
        source.mkdir(parents=True)
        write_profile(source)

        self.assertTrue(self.store.save("epos", source))

        restored = self.root / "run-2" / "profile"
        self.assertTrue(self.store.restore("epos", restored))
        self.assertEqual(
            b"SQLite format 3\x00cookie-jar",
            (restored / "Default" / "Cookies").read_bytes(),
        )
        self.assertEqual(
            b"ls",
            (restored / "Default" / "Local Storage" / "leveldb" / "000003.log").read_bytes(),
        )

    def test_only_session_data_leaves_the_machine(self) -> None:
        """Cache and browsing history are none of Drive's business."""

        source = self.root / "run-1" / "profile"
        source.mkdir(parents=True)
        write_profile(source)
        self.store.save("epos", source)

        payload = Fernet(KEY.encode()).decrypt(
            self.service.store[".getreceipt-profile-epos"]
        )
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as bundle:
            names = bundle.getnames()

        self.assertIn("Default/Cookies", names)
        self.assertNotIn("Default/History", names)
        self.assertFalse([name for name in names if name.startswith("Default/Cache")])

    def test_drive_only_ever_sees_ciphertext(self) -> None:
        source = self.root / "run-1" / "profile"
        source.mkdir(parents=True)
        write_profile(source)
        self.store.save("epos", source)

        stored = self.service.store[".getreceipt-profile-epos"]
        self.assertNotIn(b"cookie-jar", stored)

    def test_each_service_keeps_its_own_browser(self) -> None:
        for service_id in ("epos", "commufa"):
            source = self.root / service_id / "profile"
            source.mkdir(parents=True)
            write_profile(source)
            (source / "Default" / "Cookies").write_bytes(f"jar-{service_id}".encode())
            self.store.save(service_id, source)

        restored = self.root / "restored" / "profile"
        self.store.restore("commufa", restored)

        self.assertEqual(b"jar-commufa", (restored / "Default" / "Cookies").read_bytes())

    def test_a_missing_profile_is_not_an_error(self) -> None:
        self.assertFalse(self.store.restore("epos", self.root / "fresh"))

    def test_an_unreadable_profile_is_ignored_rather_than_fatal(self) -> None:
        """A key change must not stop the acquisition; it just signs in again."""

        self.service.store[".getreceipt-profile-epos"] = b"not-encrypted-by-us"

        self.assertFalse(self.store.restore("epos", self.root / "fresh"))

    def test_a_profile_with_nothing_worth_keeping_is_not_uploaded(self) -> None:
        source = self.root / "empty" / "profile"
        (source / "Default" / "Cache").mkdir(parents=True)
        (source / "Default" / "Cache" / "data_0").write_bytes(b"x")

        self.assertFalse(self.store.save("epos", source))
        self.assertEqual({}, self.service.store)

    def test_an_archive_cannot_write_outside_the_profile(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            data = b"escaped"
            info = tarfile.TarInfo("Default/Cookies/../../../escape.txt")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        self.service.store[".getreceipt-profile-epos"] = Fernet(KEY.encode()).encrypt(
            buffer.getvalue()
        )

        target = self.root / "restored"
        self.store.restore("epos", target)

        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse((target.parent / "escape.txt").exists())

    def test_an_odd_service_id_is_refused(self) -> None:
        for service_id in ("../etc", "epos/../x", ""):
            with self.subTest(service_id=service_id):
                with self.assertRaises(ValueError):
                    self.store._file_name(service_id)

    def test_forgetting_a_profile_removes_it(self) -> None:
        source = self.root / "run-1" / "profile"
        source.mkdir(parents=True)
        write_profile(source)
        self.store.save("epos", source)

        self.assertTrue(self.store.forget("epos"))
        self.assertFalse(self.store.restore("epos", self.root / "fresh"))


if __name__ == "__main__":
    unittest.main()
