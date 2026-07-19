from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / "cloud"
if str(CLOUD) not in sys.path:
    sys.path.insert(0, str(CLOUD))

from src.storage.sqlite_backup import (  # noqa: E402
    SQLiteBackupError,
    SQLiteBackupManager,
)


BACKUP_NAME = re.compile(
    r"^getreceipt-sqlite-\d{8}T\d{12}Z-[0-9a-f]{32}\.sqlite3$"
)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def create_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE receipts(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO receipts(value) VALUES ('committed')")
        connection.commit()


class SQLiteBackupManagerTest(unittest.TestCase):
    def test_online_backup_is_consistent_while_wal_writer_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "persistent"
            root.mkdir()
            database = root / "jobs.sqlite3"
            create_database(database)
            writer = sqlite3.connect(database)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO receipts(value) VALUES ('not-committed')"
                )

                result = SQLiteBackupManager(
                    database_path=database,
                    persistent_root=root,
                ).create_backup()

                with closing(sqlite3.connect(result.path)) as snapshot:
                    values = snapshot.execute(
                        "SELECT value FROM receipts ORDER BY id"
                    ).fetchall()
                    integrity = snapshot.execute("PRAGMA quick_check").fetchone()
            finally:
                writer.rollback()
                writer.close()

            self.assertEqual([("committed",)], values)
            self.assertEqual(("ok",), integrity)
            self.assertRegex(result.path.name, BACKUP_NAME)
            self.assertEqual(root / ".sqlite-backups", result.path.parent)

    def test_publish_is_atomic_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            create_database(database)
            real_replace = os.replace

            with patch(
                "src.storage.sqlite_backup.os.replace",
                wraps=real_replace,
            ) as replace:
                result = SQLiteBackupManager(
                    database_path=database,
                    persistent_root=root,
                ).create_backup()

            source, destination = replace.call_args.args
            self.assertEqual(Path(source).parent, Path(destination).parent)
            self.assertTrue(Path(source).name.startswith(".getreceipt-sqlite-"))
            self.assertEqual(result.path, Path(destination))
            self.assertTrue(result.path.is_file())
            self.assertEqual([], list(result.path.parent.glob("*.tmp")))

    def test_retention_only_deletes_strict_backup_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            create_database(database)
            manager = SQLiteBackupManager(
                database_path=database,
                persistent_root=root,
                retention_count=2,
                clock=AdvancingClock(),
            )

            results = [manager.create_backup() for _index in range(4)]
            backup_directory = manager.backup_directory
            unrelated = (
                backup_directory
                / "getreceipt-sqlite-20260719T120000000000Z-secret.sqlite3"
            )
            unrelated.write_text("do not delete", encoding="utf-8")
            note = backup_directory / "operator-note.txt"
            note.write_text("do not delete", encoding="utf-8")
            final = manager.create_backup()

            retained = sorted(
                path
                for path in backup_directory.iterdir()
                if BACKUP_NAME.fullmatch(path.name)
            )
            self.assertEqual(2, len(retained))
            self.assertIn(final.path, retained)
            self.assertGreaterEqual(final.pruned_count, 1)
            self.assertTrue(unrelated.is_file())
            self.assertTrue(note.is_file())
            self.assertFalse(results[0].path.exists())

    def test_source_must_be_a_regular_file_inside_persistent_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "persistent"
            root.mkdir()
            outside = base / "outside.sqlite3"
            create_database(outside)

            manager = SQLiteBackupManager(
                database_path=outside,
                persistent_root=root,
            )
            with self.assertRaises(SQLiteBackupError):
                manager.create_backup()

            self.assertEqual(
                [],
                list((root / ".sqlite-backups").glob("*.sqlite3")),
            )

    def test_source_name_is_never_copied_into_backup_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "password-secret-refresh-token.sqlite3"
            create_database(source)

            result = SQLiteBackupManager(
                database_path=source,
                persistent_root=root,
            ).create_backup()

            self.assertRegex(result.path.name, BACKUP_NAME)
            self.assertNotIn("password", result.path.name)
            self.assertNotIn("secret", result.path.name)
            self.assertNotIn("token", result.path.name)

    def test_corrupt_source_does_not_publish_or_leave_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            database.write_bytes(b"not a sqlite database")
            manager = SQLiteBackupManager(
                database_path=database,
                persistent_root=root,
            )

            with self.assertRaises(SQLiteBackupError):
                manager.create_backup()

            backup_directory = manager.backup_directory
            self.assertEqual([], list(backup_directory.glob("*.sqlite3")))
            self.assertEqual([], list(backup_directory.glob("*.tmp")))

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
    def test_backup_directory_and_file_are_owner_only_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            create_database(database)

            result = SQLiteBackupManager(
                database_path=database,
                persistent_root=root,
            ).create_backup()

            directory_mode = stat.S_IMODE(result.path.parent.stat().st_mode)
            file_mode = stat.S_IMODE(result.path.stat().st_mode)
            self.assertEqual(0o700, directory_mode)
            self.assertEqual(0o600, file_mode)

    def test_retention_does_not_follow_matching_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite3"
            create_database(database)
            manager = SQLiteBackupManager(
                database_path=database,
                persistent_root=root,
                retention_count=1,
                clock=AdvancingClock(),
            )
            manager.create_backup()
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            symlink = manager.backup_directory / (
                "getreceipt-sqlite-20000101T000000000000Z-"
                "00000000000000000000000000000000.sqlite3"
            )
            try:
                symlink.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")

            manager.create_backup()

            self.assertEqual("keep", outside.read_text(encoding="utf-8"))
            self.assertTrue(symlink.is_symlink())


if __name__ == "__main__":
    unittest.main()
