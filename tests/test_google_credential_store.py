from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from cryptography.fernet import Fernet  # noqa: E402

from src.storage.google_credential_store import (  # noqa: E402
    STORE_FILE_NAME,
    GoogleCredentialStore,
)


KEY = Fernet.generate_key().decode("ascii")
TOKEN = "1//0eWhLexample-refresh-token-value-1234567890"


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, blobs, *, may_write=True):
        self.blobs = blobs
        self.may_write = may_write
        self.created: list[dict] = []

    def list(self, **kwargs):
        found = [{"id": "file-1"}] if STORE_FILE_NAME in self.blobs else []
        return _Execute({"files": found})

    def get_media(self, **kwargs):
        return _Execute(self.blobs.get(STORE_FILE_NAME, b""))

    def create(self, **kwargs):
        if not self.may_write:
            raise RuntimeError("Service Accounts do not have storage quota.")
        self.created.append(kwargs["body"])
        self.blobs[STORE_FILE_NAME] = kwargs["media_body"]._fd.getvalue()
        return _Execute({"id": "file-1"})

    def update(self, **kwargs):
        self.blobs[STORE_FILE_NAME] = kwargs["media_body"]._fd.getvalue()
        return _Execute({"id": kwargs["fileId"]})


class _Permissions:
    def __init__(self):
        self.granted: list[dict] = []

    def create(self, **kwargs):
        self.granted.append(kwargs["body"])
        return _Execute({"id": "perm-1"})


class _Service:
    def __init__(self, blobs, *, may_write=True):
        self._files = _Files(blobs, may_write=may_write)
        self._permissions = _Permissions()

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


class GoogleCredentialStoreTest(unittest.TestCase):
    """Secrets can only be edited by hand, so the app keeps its own copy."""

    def setUp(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.store = GoogleCredentialStore(folder_id="folder-1", encryption_key=KEY)

    def test_a_stored_credential_comes_back(self) -> None:
        owner = _Service(self.blobs)

        self.assertTrue(self.store.save(owner, TOKEN))

        reader = _Service(self.blobs)
        self.assertEqual(TOKEN, self.store.load(reader))

    def test_drive_only_ever_sees_ciphertext(self) -> None:
        owner = _Service(self.blobs)
        self.store.save(owner, TOKEN)

        stored = self.blobs[STORE_FILE_NAME]

        self.assertNotIn(TOKEN.encode(), stored)
        blob = json.loads(Fernet(KEY.encode()).decrypt(stored).decode("utf-8"))
        self.assertEqual(TOKEN, blob["refresh_token"])

    def test_the_service_account_is_given_read_access_to_that_one_file(self) -> None:
        """It is the only credential in secrets that never expires."""

        owner = _Service(self.blobs)

        self.store.save(owner, TOKEN, share_with="worker@project.iam.gserviceaccount.com")

        granted = owner.permissions().granted
        self.assertEqual(1, len(granted))
        self.assertEqual("reader", granted[0]["role"])
        self.assertEqual(
            "worker@project.iam.gserviceaccount.com", granted[0]["emailAddress"]
        )

    def test_a_missing_credential_reads_as_absent(self) -> None:
        self.assertEqual("", self.store.load(_Service({})))

    def test_a_blob_encrypted_with_another_key_is_ignored(self) -> None:
        self.blobs[STORE_FILE_NAME] = Fernet(Fernet.generate_key()).encrypt(b"{}")

        self.assertEqual("", self.store.load(_Service(self.blobs)))

    def test_an_unusable_value_is_never_stored(self) -> None:
        owner = _Service(self.blobs)

        self.assertFalse(self.store.save(owner, ""))
        self.assertFalse(self.store.save(owner, "short"))
        self.assertEqual({}, self.blobs)

    def test_a_drive_that_refuses_the_write_is_not_fatal(self) -> None:
        owner = _Service(self.blobs, may_write=False)

        self.assertFalse(self.store.save(owner, TOKEN))

    def test_sharing_failure_still_leaves_the_credential_stored(self) -> None:
        owner = _Service(self.blobs)

        def refuse(**kwargs):
            raise RuntimeError("sharing refused")

        owner.permissions().create = refuse

        self.assertTrue(self.store.save(owner, TOKEN, share_with="sa@example.test"))
        self.assertEqual(TOKEN, self.store.load(_Service(self.blobs)))


if __name__ == "__main__":
    unittest.main()
