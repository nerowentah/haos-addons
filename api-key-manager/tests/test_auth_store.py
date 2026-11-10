"""Tests for reading the auth store: allowlisted fields only, secrets never kept (placeholder data)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import auth_store

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "auth.json"
SECRETS = ("PLACEHOLDER-SECRET", "PLACEHOLDER-HASH")


class ParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = auth_store.parse(FIXTURE.read_text(encoding="utf-8"))

    def test_secrets_never_survive_parsing(self) -> None:
        self.assertFalse(any(s in repr(self.snapshot) for s in SECRETS))

    def test_system_users_and_tokens_are_skipped(self) -> None:
        self.assertNotIn("user-system", {u.user_id for u in self.snapshot.users})
        self.assertNotIn("system-supervisor", {k.key_id for k in self.snapshot.keys})

    def test_users_are_ordered_owner_admin_then_inactive_last(self) -> None:
        self.assertEqual([u.user_id for u in self.snapshot.users], ["user-owner", "user-plain", "user-gone"])
        owner, plain, gone = self.snapshot.users
        self.assertEqual((owner.role, owner.username), ("Owner", "alex"))
        self.assertEqual((plain.role, plain.local_only), ("User", True))
        self.assertEqual((gone.role, gone.is_active), ("Read-only", False))

    def test_long_lived_and_sessions_are_split(self) -> None:
        lived = self.snapshot.keys_of("user-owner", auth_store.KIND_LONG_LIVED)
        sessions = self.snapshot.keys_of("user-owner", auth_store.KIND_SESSION)
        self.assertEqual([k.key_id for k in lived], ["llat-test", "llat-node-red"])  # newest first
        self.assertEqual([k.client_id for k in sessions], ["http://homeassistant.local:8123/"])

    def test_long_lived_expiry_is_created_plus_lifetime(self) -> None:
        key = self.snapshot.keys_of("user-plain", auth_store.KIND_LONG_LIVED)[0]
        self.assertEqual(key.name, "Grafana")
        self.assertEqual(key.expires_at, datetime(2036, 1, 13, tzinfo=UTC))
        self.assertEqual(key.last_used_ip, "192.0.2.12")

    def test_bad_documents_raise(self) -> None:
        for text in ("not json", "[]", '{"data": {}}'):
            with self.assertRaises(auth_store.AuthStoreError):
                auth_store.parse(text)


class ReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "auth"
        shutil.copy(FIXTURE, self.path)
        self.reader = auth_store.AuthReader(self.path)

    def test_reads_only_when_the_file_changes(self) -> None:
        self.assertIsNotNone(self.reader.read_if_changed())
        self.assertIsNone(self.reader.read_if_changed())
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertIsNotNone(self.reader.read_if_changed())
        self.reader.force()
        self.assertIsNotNone(self.reader.read_if_changed())

    def test_missing_file_explains_the_mapping(self) -> None:
        self.path.unlink()
        with self.assertRaisesRegex(auth_store.AuthStoreError, "mapped"):
            self.reader.read_if_changed()


if __name__ == "__main__":
    unittest.main()
