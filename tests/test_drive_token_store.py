from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.oauth.drive_token_store import DriveMicrosoftTokenStore  # noqa: E402
from src.oauth.microsoft import MicrosoftOAuthError  # noqa: E402


class _FakeExecute:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeFiles:
    """Minimal in-memory stand-in for the Drive files() resource."""

    def __init__(self, store: dict[str, bytes]):
        self._store = store
        self._counter = 0

    def list(self, *, q, fields, pageSize, supportsAllDrives, includeItemsFromAllDrives):
        files = [{"id": file_id} for file_id in self._store]
        return _FakeExecute({"files": files[:pageSize]})

    def get_media(self, *, fileId, supportsAllDrives):
        return _FakeExecute(self._store.get(fileId, b""))

    def create(self, *, body, media_body, fields, supportsAllDrives):
        self._counter += 1
        file_id = f"file-{self._counter}"
        self._store[file_id] = media_body._payload
        return _FakeExecute({"id": file_id})

    def update(self, *, fileId, media_body, supportsAllDrives):
        self._store[fileId] = media_body._payload
        return _FakeExecute({"id": fileId})


class _FakeMedia:
    def __init__(self, payload: bytes):
        self._payload = payload


class _FakeDriveService:
    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._files = _FakeFiles(self._store)

    def files(self):
        return self._files


def _store(service, key):
    store = DriveMicrosoftTokenStore(
        drive_service=service,
        folder_id="folder-1",
        encryption_key=key,
    )
    # Route media uploads through the fake payload holder.
    store._media_upload = staticmethod(lambda content: _FakeMedia(content))  # type: ignore[assignment]
    store._file_id_cache = ""
    return store


class DriveMicrosoftTokenStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Fernet.generate_key().decode("ascii")
        self.service = _FakeDriveService()

    def test_refresh_token_roundtrip_survives_new_instances(self) -> None:
        token = "r" * 40
        first = _store(self.service, self.key)
        self.assertFalse(first.connected())
        first.save_refresh_token(token)

        # A brand new instance (fresh process) reads Drive, not memory.
        second = _store(self.service, self.key)
        self.assertTrue(second.connected())
        self.assertEqual(token, second.load_refresh_token())

    def test_ciphertext_never_stores_plaintext_token(self) -> None:
        token = "secrettoken" + "z" * 30
        store = _store(self.service, self.key)
        store.save_refresh_token(token)
        blob = next(iter(self.service._store.values()))
        self.assertNotIn(b"secrettoken", blob)

    def test_pending_authorization_single_use(self) -> None:
        store = _store(self.service, self.key)
        state = "s" * 40
        verifier = "v" * 60
        store.save_pending_authorization(
            state=state,
            code_verifier=verifier,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.assertEqual(verifier, store.consume_pending_authorization(state=state))
        # A replay must fail.
        self.assertIsNone(store.consume_pending_authorization(state=state))

    def test_expired_pending_authorization_is_rejected(self) -> None:
        store = _store(self.service, self.key)
        state = "s" * 40
        store.save_pending_authorization(
            state=state,
            code_verifier="v" * 60,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.assertIsNone(store.consume_pending_authorization(state=state))

    def test_load_without_token_raises(self) -> None:
        store = _store(self.service, self.key)
        with self.assertRaises(MicrosoftOAuthError):
            store.load_refresh_token()


if __name__ == "__main__":
    unittest.main()
