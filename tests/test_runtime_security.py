from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.runtime_security import (  # noqa: E402
    WorkerInstanceLease,
    WorkerInstanceLeaseError,
    cleanup_stale_downloads,
    ensure_private_file,
    harden_private_tree,
    secure_sqlite_files,
)


class RuntimeSecurityTest(unittest.TestCase):
    def test_instance_lease_blocks_duplicate_and_recovers_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "worker.instance.lock"
            first = WorkerInstanceLease(lock_path)
            second = WorkerInstanceLease(lock_path)
            first.acquire()
            self.assertTrue(first.acquired)
            with self.assertRaises(WorkerInstanceLeaseError):
                second.acquire()

            first.release()
            self.assertFalse(first.acquired)
            second.acquire()
            self.assertTrue(second.acquired)
            second.release()

    def test_startup_cleanup_removes_only_worker_temporaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads"
            job_dir = root / "11111111-1111-4111-8111-111111111111"
            job_dir.mkdir(parents=True)
            (job_dir / "provider-attempt" / "downloads").mkdir(parents=True)
            (job_dir / "provider-attempt" / "downloads" / "receipt.pdf").write_bytes(
                b"%PDF-1.4\n"
            )
            ordinary_dir = root / "family-documents"
            ordinary_dir.mkdir()
            ordinary_file = root / "keep.pdf"
            ordinary_file.write_bytes(b"%PDF-1.4\n")
            uuid_named_file = root / "33333333-3333-4333-8333-333333333333"
            uuid_named_file.write_text("not a worker directory", encoding="utf-8")
            partial = root / "interrupted.crdownload"
            partial.write_bytes(b"partial")

            removed = cleanup_stale_downloads(root)

            self.assertEqual(2, removed)
            self.assertFalse(job_dir.exists())
            self.assertFalse(partial.exists())
            self.assertTrue(ordinary_dir.exists())
            self.assertTrue(ordinary_file.exists())
            self.assertTrue(uuid_named_file.exists())
            self.assertTrue((root / ".getreceipt-ephemeral-downloads").is_file())
            self.assertEqual(0, cleanup_stale_downloads(root))

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are required")
    def test_profile_database_and_lock_are_owner_only_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "profiles"
            nested = root / "epos"
            nested.mkdir(parents=True)
            cookie_store = nested / "Cookies"
            cookie_store.write_bytes(b"private")
            os.chmod(root, 0o777)
            os.chmod(nested, 0o755)
            os.chmod(cookie_store, 0o644)

            harden_private_tree(root)

            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(nested.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(cookie_store.stat().st_mode))

            database = Path(temp) / "data" / "jobs.sqlite3"
            database.parent.mkdir()
            database.write_bytes(b"database")
            os.chmod(database, 0o644)
            secure_sqlite_files(database)
            self.assertEqual(0o600, stat.S_IMODE(database.stat().st_mode))

            lease = WorkerInstanceLease(Path(temp) / "worker.instance.lock")
            lease.acquire()
            try:
                self.assertEqual(0o600, stat.S_IMODE(lease.path.stat().st_mode))
            finally:
                lease.release()

    @unittest.skipUnless(os.name == "posix", "symlink creation is restricted on Windows")
    def test_cleanup_unlinks_uuid_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads"
            root.mkdir()
            outside = Path(temp) / "outside"
            outside.mkdir()
            secret = outside / "keep.txt"
            secret.write_text("keep", encoding="utf-8")
            link = root / "22222222-2222-4222-8222-222222222222"
            link.symlink_to(outside, target_is_directory=True)

            self.assertEqual(1, cleanup_stale_downloads(root))

            self.assertFalse(link.exists())
            self.assertEqual("keep", secret.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "symlink creation is restricted on Windows")
    def test_private_file_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            target.write_text("secret", encoding="utf-8")
            link = Path(temp) / "link"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                ensure_private_file(link)


if __name__ == "__main__":
    unittest.main()
