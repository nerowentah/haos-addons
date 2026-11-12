"""Tests for adding http: trusted_proxies to configuration.yaml (text edits only, no Home Assistant)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import ha_proxy
import supervisor

NET = "172.30.33.0/24"


class EnsureTest(unittest.TestCase):
    """ensure_trusted_proxy(): which configurations are edited, and how."""

    def check(self, text: str) -> tuple[str | None, str]:
        return ha_proxy.ensure_trusted_proxy(text, NET)

    def test_no_http_block_appends_one(self) -> None:
        new, status = self.check("default_config:\n\nautomation: !include automations.yaml\n")
        self.assertEqual(status, ha_proxy.STATUS_ADDED)
        assert new is not None
        self.assertTrue(new.startswith("default_config:\n\nautomation: !include automations.yaml\n\n# Added by"))
        self.assertTrue(new.endswith(f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - {NET}\n"))

    def test_a_file_without_a_final_newline_and_crlf_line_ends(self) -> None:
        new, _status = self.check("default_config:\r\nhomeassistant:\r\n  name: Home")
        assert new is not None
        self.assertIn("  name: Home\r\n\r\n# Added by", new)
        self.assertNotIn("\n\n# Added", new.replace("\r\n", "~"))  # only CRLF line ends were added
        self.assertTrue(new.endswith(f"    - {NET}\r\n"))

    def test_an_http_block_without_the_keys_gets_them_at_its_indent(self) -> None:
        text = "http:\n    server_port: 8123\n    ip_ban_enabled: true\nlogger:\n  default: info\n"
        new, status = self.check(text)
        self.assertEqual(status, ha_proxy.STATUS_ADDED)
        self.assertEqual(
            new,
            "http:\n    use_x_forwarded_for: true\n    trusted_proxies:\n      - 172.30.33.0/24\n"
            "    server_port: 8123\n    ip_ban_enabled: true\nlogger:\n  default: info\n",
        )

    def test_an_existing_proxy_list_gets_our_network_appended(self) -> None:
        text = "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 192.168.1.10  # nginx\n    - '10.0.0.0/8'\n  server_port: 8123\n"
        new, status = self.check(text)
        self.assertEqual(status, ha_proxy.STATUS_ADDED)
        self.assertIn("    - '10.0.0.0/8'\n    - 172.30.33.0/24\n  server_port: 8123\n", new or "")

    def test_only_the_missing_forwarded_key_is_added(self) -> None:
        new, status = self.check("http:\n  trusted_proxies:\n    - 172.30.33.0/24\n")
        self.assertEqual(status, ha_proxy.STATUS_ADDED)
        self.assertEqual(new, "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 172.30.33.0/24\n")

    def test_already_trusted_is_left_alone(self) -> None:
        for text in (
            "http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - 172.30.33.0/24\n",
            "http:\n  use_x_forwarded_for: yes\n  trusted_proxies:\n  - 172.30.32.0/23\n",  # a wider net, list at key indent
            "http:\n  use_x_forwarded_for: true\n  trusted_proxies: [127.0.0.1, 172.30.32.0/23]\n",
            "http:\n  use_x_forwarded_for: true\n  trusted_proxies: 172.30.33.0/24\n",
        ):
            self.assertEqual(self.check(text), (None, ha_proxy.STATUS_PRESENT), text)

    def test_unsafe_layouts_are_left_to_the_user(self) -> None:
        for text in (
            "http: !include http.yaml\n",
            "http: {server_port: 8123}\n",
            "http:\n  use_x_forwarded_for: false\n",
            "http:\n  use_x_forwarded_for: true\n  trusted_proxies: [10.0.0.1]\n",
            "http:\n\tserver_port: 8123\n",
        ):
            self.assertEqual(self.check(text), (None, ha_proxy.STATUS_MANUAL), text)

    def test_a_nested_http_key_is_not_the_top_level_block(self) -> None:
        new, status = self.check("sensor:\n  - platform: rest\n    http:\n      timeout: 5\n")
        self.assertEqual(status, ha_proxy.STATUS_ADDED)
        self.assertTrue((new or "").endswith(f"http:\n  use_x_forwarded_for: true\n  trusted_proxies:\n    - {NET}\n"))

    def test_network_for_the_app_address(self) -> None:
        self.assertEqual(ha_proxy.network_for("172.30.33.7"), "172.30.33.0/24")
        self.assertEqual(ha_proxy.network_for(None), ha_proxy.DEFAULT_NETWORK)
        self.assertEqual(ha_proxy.network_for("not-an-ip"), ha_proxy.DEFAULT_NETWORK)


class FakeCheck(supervisor.Supervisor):
    def __init__(self, valid: bool | None) -> None:
        super().__init__("token")
        self.valid = valid

    def check_core_config(self) -> tuple[bool, str]:
        if self.valid is None:
            raise supervisor.SupervisorError("HTTP 502")
        return self.valid, "" if self.valid else "Invalid config for [http]"


class ApplyTest(unittest.TestCase):
    """apply(): backup, check, restore."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "configuration.yaml"
        self.original = "default_config:\n"
        self.path.write_text(self.original, encoding="utf-8")
        self.backup = self.path.with_name(self.path.name + ha_proxy.BACKUP_SUFFIX)

    def test_valid_change_is_kept_with_a_backup(self) -> None:
        result = ha_proxy.apply(FakeCheck(valid=True), NET, self.path)
        self.assertEqual(result.status, ha_proxy.STATUS_ADDED)
        self.assertIn("Restart Home Assistant", result.message)
        self.assertIn("trusted_proxies", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.backup.read_text(encoding="utf-8"), self.original)

    def test_invalid_change_is_rolled_back(self) -> None:
        result = ha_proxy.apply(FakeCheck(valid=False), NET, self.path)
        self.assertEqual(result.status, ha_proxy.STATUS_INVALID)
        self.assertIn("Invalid config", result.message)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_no_config_check_means_no_change(self) -> None:
        result = ha_proxy.apply(FakeCheck(valid=None), NET, self.path)
        self.assertEqual(result.status, ha_proxy.STATUS_ERROR)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)

    def test_present_and_manual_never_write(self) -> None:
        self.path.write_text("http: !include http.yaml\n", encoding="utf-8")
        result = ha_proxy.apply(FakeCheck(valid=True), NET, self.path)
        self.assertEqual(result.status, ha_proxy.STATUS_MANUAL)
        self.assertIn(f"- {NET}", result.message)  # the snippet to paste
        self.assertFalse(self.backup.exists())

    def test_a_missing_file_asks_for_the_snippet(self) -> None:
        self.path.unlink()
        result = ha_proxy.apply(FakeCheck(valid=True), NET, self.path)
        self.assertEqual(result.status, ha_proxy.STATUS_MANUAL)


if __name__ == "__main__":
    unittest.main()
