"""Tests for !secret references: resolved for use, never written back as values (no network)."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

import config
import main
from core import store, supervisor, webui

REPO = Path(__file__).resolve().parents[3]
STORED = {
    "tnb_mytnb_username": "you@example.com",
    "tnb_mytnb_password": "!secret tnb_pw",
    "tnb_calendarific_api_key": "!secret calendarific",
    "mqtt": True,
    "tnb_accounts": [
        {"name": "Home", "tariff": "general", "billing_start_day": 1, "account_number": "123456789012"},
        {"name": "Shop", "tariff": "general", "billing_start_day": 1, "account_number": "123456789013",
         "mytnb_username": "shop@example.com", "mytnb_password": "!secret shop_pw"},
    ],
}  # fmt: skip
RESOLVED = copy.deepcopy(STORED)
RESOLVED["tnb_mytnb_password"] = "hunter2"
RESOLVED["tnb_calendarific_api_key"] = "key-123"
RESOLVED["tnb_accounts"][1]["mytnb_password"] = "shop-pass"  # type: ignore[index]


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
            self.secrets.resolve({**STORED, "tnb_mytnb_password": "!secret new_pw"})
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
        paths = [*REPO.glob("*/rootfs/app/webui.py"), *REPO.glob("*/rootfs/app/core/webui.py")]
        self.assertGreater(len(paths), 1)
        self.assertEqual(len({p.read_bytes().replace(b"\r\n", b"\n") for p in paths}), 1)


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
            config.load_config(secrets.resolve(raw)), self.sup, store.Store(Path(tmp.name) / "s.json"), raw
        )  # type: ignore[arg-type]
        self.app.secrets = secrets
        self.app.broker_found = False

    def tnb(self) -> object:
        return self.app.config.providers["tnb"]

    def test_live_option_change_keeps_the_resolved_password(self) -> None:
        self.sup.stored["tnb_afa_rate"] = 0.01
        self.app.check_options()
        self.assertEqual(self.tnb().afa_rate, 0.01)  # type: ignore[attr-defined]
        self.assertEqual(self.tnb().accounts[0].mytnb_password, "hunter2")  # type: ignore[attr-defined]
        self.assertEqual(self.tnb().accounts[1].mytnb_password, "shop-pass")  # type: ignore[attr-defined]
        self.assertEqual(self.app.engines["tnb"].config.afa_rate, 0.01)  # type: ignore[attr-defined]

    def test_panel_switch_writes_references_not_values(self) -> None:
        self.app.panel_toggle("mqtt", False)
        written = self.sup.written[-1]
        self.assertEqual(written["tnb_mytnb_password"], "!secret tnb_pw")
        self.assertEqual(written["tnb_accounts"][1]["mytnb_password"], "!secret shop_pw")  # type: ignore[index]
        self.assertNotIn("hunter2", str(written))
        self.assertIs(written["mqtt"], False)

    def test_provider_switch_writes_references_not_values(self) -> None:
        self.app.panel_toggle("tnb:account:shop:enabled", False)
        written = self.sup.written[-1]
        self.assertIs(written["tnb_accounts"][1]["enabled"], False)  # type: ignore[index]
        self.assertEqual(written["tnb_accounts"][1]["mytnb_password"], "!secret shop_pw")  # type: ignore[index]
        self.assertNotIn("shop-pass", str(written))

    def test_panel_switch_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("mqtt", False)
        self.assertEqual(self.sup.written, [])

    def test_new_secret_after_start_is_not_used_as_a_password(self) -> None:
        self.sup.stored["tnb_mytnb_password"] = "!secret brand_new"
        with self.assertLogs("malaysia_utility_bills", "ERROR"):
            self.app.check_options()
        self.assertEqual(self.tnb().accounts[0].mytnb_password, "hunter2")  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
