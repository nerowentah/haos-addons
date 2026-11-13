"""Tests for the configuration.yaml edits (text only, no Home Assistant)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import ha_config
import supervisor

NET = "172.30.33.0/24"
CERT = "/ssl/ssl-cert-manager/default/fullchain.pem"
KEY = "/ssl/ssl-cert-manager/default/privkey.pem"


class ProxyTest(unittest.TestCase):
    def test_no_http_block_appends_one(self) -> None:
        new, status = ha_config.ensure_trusted_proxy("default_config:\n", NET)
        self.assertEqual(status, ha_config.STATUS_ADDED)
        assert new is not None
        self.assertTrue(new.endswith(f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - {NET}\n"))

    def test_wider_network_is_enough(self) -> None:
        text = "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 172.30.32.0/23\n"
        self.assertEqual(ha_config.ensure_trusted_proxy(text, NET), (None, ha_config.STATUS_PRESENT))

    def test_missing_entry_is_added_to_the_list(self) -> None:
        text = "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 10.0.0.1\nlogger:\n"
        new, status = ha_config.ensure_trusted_proxy(text, NET)
        self.assertEqual(status, ha_config.STATUS_ADDED)
        self.assertEqual(
            new, f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 10.0.0.1\n    - {NET}\nlogger:\n"
        )

    def test_include_and_explicit_off_are_left_alone(self) -> None:
        for text in ("http: !include http.yaml\n", "http:\n  use_x_forwarded_for: false\n"):
            with self.subTest(text=text):
                self.assertEqual(ha_config.ensure_trusted_proxy(text, NET), (None, ha_config.STATUS_MANUAL))


class SslTest(unittest.TestCase):
    def test_append_and_remove_round_trip(self) -> None:
        original = "default_config:\n\nautomation: !include automations.yaml\n"
        added, status = ha_config.ensure_ssl(original, CERT, KEY)
        self.assertEqual(status, ha_config.STATUS_ADDED)
        assert added is not None
        self.assertIn(f"http:\n  ssl_certificate: {CERT}\n  ssl_key: {KEY}\n", added)
        removed, status = ha_config.remove_ssl(added, CERT, KEY)
        self.assertEqual(status, ha_config.STATUS_REMOVED)
        self.assertEqual(removed, original)

    def test_existing_block_keeps_its_keys_and_indent(self) -> None:
        text = "http:\n    server_port: 8123\n    ip_ban_enabled: true\nlogger:\n  default: info\n"
        added, status = ha_config.ensure_ssl(text, CERT, KEY)
        self.assertEqual(status, ha_config.STATUS_ADDED)
        assert added is not None
        self.assertTrue(added.startswith(f"http:\n    ssl_certificate: {CERT}{ha_config.SSL_MARK}\n    ssl_key: {KEY}"))
        self.assertIn("    server_port: 8123\n", added)
        removed, status = ha_config.remove_ssl(added, CERT, KEY)
        self.assertEqual((removed, status), (text, ha_config.STATUS_REMOVED))

    def test_already_present(self) -> None:
        text = f"http:\n  ssl_certificate: {CERT}\n  ssl_key: '{KEY}'\n"
        self.assertEqual(ha_config.ensure_ssl(text, CERT, KEY), (None, ha_config.STATUS_PRESENT))

    def test_someone_elses_certificate_is_not_touched(self) -> None:
        text = "http:\n  ssl_certificate: /ssl/fullchain.pem\n  ssl_key: /ssl/privkey.pem\n"
        self.assertEqual(ha_config.ensure_ssl(text, CERT, KEY), (None, ha_config.STATUS_MANUAL))
        self.assertEqual(ha_config.remove_ssl(text, CERT, KEY), (None, ha_config.STATUS_MANUAL))

    def test_crlf_is_kept(self) -> None:
        text = "default_config:\r\nhttp:\r\n  server_port: 8123\r\n"
        added, _status = ha_config.ensure_ssl(text, CERT, KEY)
        assert added is not None
        self.assertNotIn("\n", added.replace("\r\n", ""))

    def test_remove_when_nothing_is_there(self) -> None:
        self.assertEqual(ha_config.remove_ssl("default_config:\n", CERT, KEY), (None, ha_config.STATUS_PRESENT))


class FakeSupervisor(supervisor.Supervisor):
    def __init__(self, valid: bool = True) -> None:
        super().__init__("token")
        self.valid = valid

    def check_core_config(self) -> tuple[bool, str]:
        return self.valid, "" if self.valid else "Invalid config for [http]"


class ApplyTest(unittest.TestCase):
    def test_failed_check_restores_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.yaml"
            path.write_text("default_config:\n", encoding="utf-8")
            result = ha_config.apply(
                FakeSupervisor(valid=False),
                lambda t: ha_config.ensure_ssl(t, CERT, KEY),
                ha_config.ssl_snippet(CERT, KEY),
                path,
            )
            self.assertEqual(result.status, ha_config.STATUS_INVALID)
            self.assertEqual(path.read_text(encoding="utf-8"), "default_config:\n")
            self.assertTrue((Path(tmp) / "configuration.yaml.ssl-cert-manager.bak").exists())

    def test_success_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "configuration.yaml"
            missing = ha_config.apply(FakeSupervisor(), lambda t: ha_config.ensure_ssl(t, CERT, KEY), "x", path)
            self.assertEqual(missing.status, ha_config.STATUS_MANUAL)
            path.write_text("default_config:\n", encoding="utf-8")
            result = ha_config.apply(FakeSupervisor(), lambda t: ha_config.ensure_ssl(t, CERT, KEY), "x", path)
            self.assertEqual(result.status, ha_config.STATUS_ADDED)
            self.assertIn("ssl_key", path.read_text(encoding="utf-8"))
            self.assertIn("Restart Home Assistant", result.message)


if __name__ == "__main__":
    unittest.main()
