"""Tests for option changes: !secret references kept, nothing written without the Supervisor (no network)."""

from __future__ import annotations

import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
if "websocket" not in sys.modules:  # websocket-client ships in the app image; these tests never open a socket
    stub = types.ModuleType("websocket")
    vars(stub)["WebSocketException"] = type("WebSocketException", (Exception,), {})
    sys.modules["websocket"] = stub

import config
import main
import store
import supervisor
import webui

STORED: dict[str, object] = {
    "scan_minutes": 30,
    "stale_days": 30,
    "notify_on_attention": True,
    "ignored_devices": ["!secret tablet"],
}
RESOLVED: dict[str, object] = {**STORED, "ignored_devices": ["abc123"]}


class FakeSupervisor(supervisor.Supervisor):
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


class OptionsTest(unittest.TestCase):
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

    def test_live_change_resolves_references(self) -> None:
        self.sup.stored["stale_days"] = 10
        self.app.check_options()
        self.assertEqual(self.app.config.stale_days, 10)
        self.assertEqual(self.app.config.ignored_devices, ("abc123",))

    def test_panel_change_writes_references(self) -> None:
        self.app.update_options(lambda raw: raw.update(stale_days=5))
        self.assertEqual(self.sup.written[-1]["ignored_devices"], ["!secret tablet"])

    def test_panel_change_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.update_options(lambda raw: raw.update(stale_days=5))
        self.assertEqual(self.sup.written, [])

    def test_ignore_button_keeps_references(self) -> None:
        self.app.panel_action("ignore", webui.PanelArgs(device="d9", value=True))
        self.assertEqual(self.sup.written[-1]["ignored_devices"], ["!secret tablet", "d9"])
        self.sup.stored = self.sup.written[-1]
        self.app.panel_action("ignore", webui.PanelArgs(device="d9", value=False))
        self.assertEqual(self.sup.written[-1]["ignored_devices"], ["!secret tablet"])

    def test_malformed_button_requests_are_refused(self) -> None:
        for args in ({}, {"device": ""}, {"device": ["d9"]}, {"device": "d9", "value": "yes"}):
            with self.assertRaises(webui.PanelError):
                self.app.panel_action("ignore", webui.PanelArgs(args))
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("remove", webui.PanelArgs())
        self.assertEqual(self.sup.written, [])

    def test_scan_interval_goes_up_to_a_week(self) -> None:
        self.assertEqual(config.load_config({"scan_minutes": 99999}).scan_minutes, 10080)


if __name__ == "__main__":
    unittest.main()
