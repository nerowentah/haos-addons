"""Tests for the recorder database: its size is only read, and the outcome of a purge comes from watching it."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import database
from helpers import GB, FakeSupervisor


class SizeTest(unittest.TestCase):
    def test_size_adds_the_side_files_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(database.size(root))
            (root / database.DB_NAME).write_bytes(b"a" * 1000)
            (root / f"{database.DB_NAME}-wal").write_bytes(b"b" * 200)
            found = database.size(root)
            assert found is not None
            self.assertEqual((found.main, found.wal, found.shm, found.total), (1000, 200, 0, 1200))
            self.assertEqual((root / database.DB_NAME).read_bytes(), b"a" * 1000)


class PurgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sup = FakeSupervisor()

    def test_purge_is_the_only_service_called(self) -> None:
        self.assertTrue(database.purge(self.sup, repack=False))
        self.assertTrue(database.purge(self.sup, repack=True))
        self.assertEqual(self.sup.purges(), [{"repack": False}, {"repack": True}])
        self.assertEqual(
            [(method, path, timeout) for method, path, _body, timeout in self.sup.calls],
            [("POST", "/core/api/services/recorder/purge", database.PURGE_TIMEOUT)] * 2,
        )

    def test_running_out_of_time_is_not_a_failure(self) -> None:
        for error in ("POST /core/api/services/recorder/purge failed: timed out", "HTTP 504 from POST x"):
            self.sup.purge_error = error
            with self.subTest(error=error):
                self.assertFalse(database.purge(self.sup, repack=True))

    def test_a_refusal_is_an_error(self) -> None:
        self.sup.purge_error = "HTTP 400 from POST /core/api/services/recorder/purge"
        with self.assertRaises(database.DatabaseError):
            database.purge(self.sup, repack=False)


class WatchTest(unittest.TestCase):
    def test_a_repack_is_reported_once_the_size_has_settled(self) -> None:
        watch = database.Watch.start("Purge and repack", 4 * GB, 0.0, repack=True)
        self.assertIsNone(watch.update(30, 4 * GB))
        self.assertIsNone(watch.update(400, 5 * GB))  # the write-ahead log grows while it works
        self.assertIsNone(watch.update(430, 3 * GB))
        self.assertIsNone(watch.update(600, 3 * GB))
        outcome = watch.update(430 + database.SETTLE_SECONDS, 3 * GB)
        assert outcome is not None
        self.assertEqual(outcome[1], "ok")
        self.assertIn("4.0 GB to 3.0 GB", outcome[0])
        self.assertIn("1.0 GB freed", outcome[0])

    def test_a_call_that_ran_out_of_time_still_gets_its_real_outcome(self) -> None:
        sup = FakeSupervisor()
        sup.purge_error = "POST /core/api/services/recorder/purge failed: timed out"
        self.assertFalse(database.purge(sup, repack=True))
        watch = database.Watch.start("Purge and repack", 2 * GB, 310.0, repack=True)
        self.assertIsNone(watch.update(340, 2 * GB))
        self.assertIsNone(watch.update(900, GB))
        outcome = watch.update(900 + database.SETTLE_SECONDS, GB)
        assert outcome is not None
        self.assertEqual(outcome[1], "ok")

    def test_a_repack_that_changes_nothing_is_a_warning(self) -> None:
        watch = database.Watch.start("Purge and repack", 4 * GB, 0.0, repack=True)
        self.assertIsNone(watch.update(database.IDLE_SECONDS - 1, 4 * GB))
        outcome = watch.update(database.IDLE_SECONDS, 4 * GB)
        assert outcome is not None
        self.assertEqual(outcome[1], "warn")

    def test_a_purge_alone_that_keeps_the_size_is_expected(self) -> None:
        watch = database.Watch.start("Purge", 4 * GB, 0.0, repack=False)
        outcome = watch.update(database.IDLE_SECONDS, 4 * GB)
        assert outcome is not None
        self.assertEqual(outcome[1], "neutral")
        self.assertIn("expected", outcome[0])

    def test_a_database_that_never_settles_is_reported_after_the_limit(self) -> None:
        watch = database.Watch.start("Purge and repack", 4 * GB, 0.0, repack=True)
        size, now = 4 * GB, 0.0
        while now < database.MAX_SECONDS - 60:
            now += 60
            size += 1
            self.assertIsNone(watch.update(now, size))
        outcome = watch.update(database.MAX_SECONDS, size + 1)
        assert outcome is not None
        self.assertEqual(outcome[1], "warn")
        self.assertIn("still working", outcome[0])

    def test_a_database_that_disappears_ends_the_watch(self) -> None:
        watch = database.Watch.start("Purge", 4 * GB, 0.0, repack=False)
        outcome = watch.update(30, None)
        assert outcome is not None
        self.assertEqual(outcome[1], "warn")


if __name__ == "__main__":
    unittest.main()
