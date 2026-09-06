from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.instance_lock import InstanceAlreadyRunningError, InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_only_one_core_can_own_a_database_and_release_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            first = InstanceLock.for_database(database)
            try:
                with self.assertRaises(InstanceAlreadyRunningError):
                    InstanceLock.for_database(database)
            finally:
                first.close()

            replacement = InstanceLock.for_database(database)
            replacement.close()

    def test_lock_file_is_private_and_contains_owner_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            with InstanceLock.for_database(database) as lock:
                self.assertEqual(lock.path.stat().st_mode & 0o777, 0o600)
                self.assertTrue(lock.path.read_text(encoding="ascii").strip().isdigit())
