"""Tests for !secret references: resolved for use, never written back as values (no network)."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import main
import store
import supervisor
import webui

REPO = Path(__file__).resolve().parents[2]
HOME: dict[str, object] = {
    "name": "Home",
    "tariff": "general",
    "billing_start_day": 1,
    "account_number": "123456789012",
}
SHOP: dict[str, object] = {
    "name": "Shop",
    "tariff": "general",
    "billing_start_day": 1,
    "account_number": "123456789013",
    "mytnb_username": "shop@example.com",
    "mytnb_password": "!secret shop_pw",
}
STORED: dict[str, object] = {
    "mytnb_username": "you@example.com",
    "mytnb_password": "!secret tnb_pw",
    "calendarific_api_key": "!secret calendarific",
    "mqtt": True,
    "accounts": [
        HOME,
        SHOP,
    ],
}
RESOLVED: dict[str, object] = {
    **copy.deepcopy(STORED),
    "mytnb_password": "hunter2",
    "calendarific_api_key": "key-123",
    "accounts": [copy.deepcopy(HOME), {**SHOP, "mytnb_password": "shop-pass"}],
}


class SecretRefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.secrets = webui.SecretRefs()
        self.secrets.learn(STORED, RESOLVED)

    def test_learns_top_level_and_nested_references(self) -> None:
        self.assertEqual(len(self.secrets), 3)
        self.assertEqual(self.secrets.resolve(STORED), RESOLVED)

    def test_resolve_does_not_touch_the_stored_options(self) -> None:
        before = copy.deepcopy(STORED)
        self.secrets.resolve(STORED)
        self.assertEqual(STORED, before)

    def test_unknown_reference_asks_for_a_restart(self) -> None:
        with self.assertRaises(webui.SecretError) as ctx:
            self.secrets.resolve({**STORED, "mytnb_password": "!secret new_pw"})
        self.assertIn("restart", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_plain_values_pass_through(self) -> None:
        self.assertEqual(webui.SecretRefs().resolve({"a": [1, "x", {"b": None}]}), {"a": [1, "x", {"b": None}]})

    def test_initial_options_prefers_the_stored_copy(self) -> None:
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: RESOLVED, lambda: STORED, secrets)
        self.assertEqual(raw, STORED)
        self.assertEqual(len(secrets), 3)

    def test_initial_options_without_the_supervisor_uses_the_file(self) -> None:
        def down() -> dict[str, object]:
            raise supervisor.SupervisorError("unreachable")

        self.assertEqual(webui.initial_options(lambda: RESOLVED, down, webui.SecretRefs()), RESOLVED)

    def test_every_app_ships_the_same_webui(self) -> None:
        copies = {p.read_bytes() for p in REPO.glob("*/rootfs/app/webui.py")}
        self.assertEqual(len(copies), 1)


class FakeSupervisor(supervisor.Supervisor):
    """Stored options with !secret references; records writes."""

    def __init__(self) -> None:
        super().__init__("token")
        self.stored = copy.deepcopy(STORED)
        self.reachable = True
        self.written: list[dict[str, object]] = []

    def fetch_options(self) -> dict[str, object]:
        if not self.reachable:
            raise supervisor.SupervisorError("unreachable")
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(copy.deepcopy(options))
        self.stored = copy.deepcopy(options)

    def core_post(self, path: str, body: object) -> object:  # noqa: ARG002
        return None


class AppSecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sup = FakeSupervisor()
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: copy.deepcopy(RESOLVED), self.sup.fetch_options, secrets)
        self.app = main.App(
            config.load_config(secrets.resolve_options(raw)), self.sup, store.Store(Path(tmp.name) / "s.json"), raw
        )
        self.app.secrets = secrets
        self.app.broker_found = False

    def test_live_option_change_keeps_the_resolved_password(self) -> None:
        self.sup.stored["afa_rate"] = 0.01
        self.app.check_options()
        self.assertEqual(self.app.config.afa_rate, 0.01)
        self.assertEqual(self.app.config.accounts[0].mytnb_password.reveal(), "hunter2")
        self.assertEqual(self.app.config.accounts[1].mytnb_password.reveal(), "shop-pass")
        self.assertNotIn("hunter2", repr(self.app.config))
        self.assertNotIn("key-123", repr(self.app.config))

    def test_panel_switch_writes_references_not_values(self) -> None:
        self.app.panel_toggle("mqtt", False)
        written = self.sup.written[-1]
        self.assertEqual(written["mytnb_password"], "!secret tnb_pw")
        self.assertEqual(written["accounts"], STORED["accounts"])
        self.assertNotIn("hunter2", str(written))
        self.assertIs(written["mqtt"], False)

    def test_panel_switch_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("mqtt", False)
        self.assertEqual(self.sup.written, [])

    def test_new_secret_after_start_is_not_used_as_a_password(self) -> None:
        self.sup.stored["mytnb_password"] = "!secret brand_new"
        with self.assertLogs("power_tnb", "ERROR"):
            self.app.check_options()
        self.assertEqual(self.app.config.accounts[0].mytnb_password.reveal(), "hunter2")

    def test_overrides_migration_writes_references(self) -> None:
        self.sup.stored["notification_overrides"] = [{"name": "mobile_app_a", "notify": False}]
        self.app.raw_options = self.sup.fetch_options()
        self.app.migrate_overrides()
        self.assertEqual(self.sup.written[-1]["mytnb_password"], "!secret tnb_pw")
        self.assertNotIn("notification_overrides", self.sup.written[-1])


if __name__ == "__main__":
    unittest.main()
