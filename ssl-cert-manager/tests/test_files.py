"""Tests for files.py: /data originals, the /ssl tree and the /ssl root ownership rule."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import files
from helpers import make_ca, make_leaf


class FilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ssl = base / "ssl"
        self.ssl.mkdir()
        self.files = files.Files(base / "data", self.ssl)
        ca = make_ca()
        self.leaf, self.key = make_leaf(["home.example.com"], ca)
        self.chain = [self.leaf, ca[0]]
        self.files.save_cert("home-example-com", self.chain, self.key)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sync(self, ids: list[str], default: str | None, *, root: bool = True) -> files.SyncResult:
        return self.files.sync(ids, default, "TRUST\n", {"version": 1}, write_root=root)

    def test_round_trip_and_key_permissions(self) -> None:
        chain, _key = self.files.load_cert("home-example-com")
        self.assertEqual(chain[0], self.leaf)
        self.assertEqual(len(chain), 2)
        mode = (self.files.certs_dir / "home-example-com" / "privkey.pem").stat().st_mode & 0o777
        self.assertIn(mode, (0o600, 0o666))  # 0o666 on Windows, where chmod only knows read-only

    def test_publish_default_and_root(self) -> None:
        result = self.sync(["home-example-com"], "home-example-com")
        self.assertTrue(result.changed)
        share = self.ssl / "ssl-cert-manager"
        for folder in ("home-example-com", "default"):
            self.assertTrue((share / folder / "fullchain.pem").is_file())
            self.assertTrue((share / folder / "privkey.pem").is_file())
        self.assertEqual((self.ssl / "fullchain.pem").read_bytes(), (share / "default" / "fullchain.pem").read_bytes())
        self.assertEqual(json.loads((share / "status.json").read_text()), {"version": 1})
        self.assertEqual((share / "trust" / "ca-bundle.pem").read_text(), "TRUST\n")
        self.assertFalse(self.sync(["home-example-com"], "home-example-com").changed)  # idempotent

    def test_foreign_root_files_are_never_replaced(self) -> None:
        (self.ssl / "fullchain.pem").write_text("someone else's\n")
        result = self.sync(["home-example-com"], "home-example-com")
        self.assertIsNotNone(result.root_note)
        self.assertEqual((self.ssl / "fullchain.pem").read_text(), "someone else's\n")
        self.assertFalse((self.ssl / "privkey.pem").exists())

    def test_turning_root_off_removes_only_our_files(self) -> None:
        self.sync(["home-example-com"], "home-example-com")
        self.sync(["home-example-com"], "home-example-com", root=False)
        self.assertFalse((self.ssl / "fullchain.pem").exists())
        (self.ssl / "fullchain.pem").write_text("someone else's\n")
        self.sync(["home-example-com"], "home-example-com", root=False)
        self.assertTrue((self.ssl / "fullchain.pem").exists())

    def test_a_changed_root_file_is_no_longer_ours(self) -> None:
        self.sync(["home-example-com"], "home-example-com")
        (self.ssl / "fullchain.pem").write_text("replaced by the user\n")
        self.sync(["home-example-com"], "home-example-com", root=False)
        self.assertEqual((self.ssl / "fullchain.pem").read_text(), "replaced by the user\n")

    def test_deleted_certificates_disappear(self) -> None:
        self.sync(["home-example-com"], "home-example-com")
        self.files.delete_cert("home-example-com")
        result = self.sync([], None)
        self.assertTrue(result.changed)
        share = self.ssl / "ssl-cert-manager"
        self.assertFalse((share / "home-example-com").exists())
        self.assertFalse((share / "default").exists())
        self.assertFalse((self.ssl / "fullchain.pem").exists())
        self.assertTrue((share / "trust" / "ca-bundle.pem").exists())

    def test_ca_with_and_without_key(self) -> None:
        ca_cert, ca_key = make_ca("Imported")
        self.files.save_ca("ca-imported", ca_cert, ca_key)
        cert, key = self.files.load_ca("ca-imported")
        self.assertEqual(cert, ca_cert)
        self.assertIsNotNone(key)
        self.files.save_ca("ca-trust", ca_cert, None)
        self.assertIsNone(self.files.load_ca("ca-trust")[1])


if __name__ == "__main__":
    unittest.main()
