"""Tests for the app: options with !secret references, restart decisions, failure notifications, panel actions (no network)."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import cloudflared as cf
import config
import main
import store
import supervisor
import webui

STORED = {"tunnel_token": "!secret ct", "notify_on_failure": True, "run_parameters": [], "log_level": "info"}
RESOLVED = {**STORED, "tunnel_token": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.secret-value"}
TOKEN = str(RESOLVED["tunnel_token"])


class FakeSupervisor(supervisor.Supervisor):
    def __init__(self) -> None:
        super().__init__("token")
        self.stored = copy.deepcopy(STORED)
        self.reachable = True
        self.written: list[dict[str, object]] = []
        self.sent: list[tuple[list[str], str, str]] = []
        self.targets: list[str] | None = []

    def fetch_options(self) -> dict[str, object]:
        if not self.reachable:
            raise supervisor.SupervisorError("unreachable")
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(copy.deepcopy(options))
        self.stored = copy.deepcopy(options)

    def discover_mobile_targets(self) -> list[str] | None:
        if self.targets is None:
            return None
        return list(self.targets)

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.sent.append((list(targets), title, message))


class FakeDaemon:
    """Stands in for cloudflared.Cloudflared: a status object plus recorded process control."""

    def __init__(self, cfg: config.Config) -> None:
        self.config = cfg
        self.status = cf.TunnelStatus()
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def restart(self) -> None:
        self.calls.append("restart")
        self.status.restarts += 1

    def poll(self) -> None:
        self.calls.append("poll")


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.sup = FakeSupervisor()
        secrets = webui.SecretRefs()
        secrets.learn(STORED, RESOLVED)
        self.store = store.Store(self.dir / "state.json")
        self.store.load()
        self.daemon = FakeDaemon(config.load_config(secrets.resolve(copy.deepcopy(STORED))))
        self.app = main.App(
            config.load_config(secrets.resolve(copy.deepcopy(STORED))),
            self.sup,
            self.store,
            copy.deepcopy(STORED),
            daemon=self.daemon,  # type: ignore[arg-type]
        )
        self.app.secrets = secrets

    def _state_text(self) -> str:
        return json.dumps(self.app.panel_state())

    # -- The token stays secret -------------------------------------------------------
    def test_the_panel_never_shows_the_token(self) -> None:
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.daemon.status.connections = 4
        self.daemon.status.version = "2026.10.7"
        text = self._state_text()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("!secret", text)
        self.assertIn("Connected", text)
        self.assertIn("2026.10.7", text)
        self.assertIn("Edge connections", text)

    # -- Applying options ---------------------------------------------------------------
    def test_the_notification_switch_alone_does_not_restart_the_connector(self) -> None:
        quieter = config.load_config({**RESOLVED, "notify_on_failure": False})
        self.app.apply_config(copy.deepcopy({**STORED, "notify_on_failure": False}), quieter)
        self.assertFalse(self.app.config.notify_on_failure)
        self.assertEqual(self.daemon.calls, [])

    def test_a_new_token_restarts_the_connector(self) -> None:
        rotated = config.load_config({"tunnel_token": "rotated-token"})
        self.app.apply_config({"tunnel_token": "rotated-token"}, rotated)
        self.assertIn("restart", self.daemon.calls)
        self.assertEqual(self.daemon.config, rotated)

    def test_a_new_log_level_restarts_the_connector(self) -> None:
        self.addCleanup(config.apply_log_level, "info")
        louder = config.load_config({**RESOLVED, "log_level": "debug"})
        self.app.apply_config(copy.deepcopy({**STORED, "log_level": "debug"}), louder)
        self.assertIn("restart", self.daemon.calls)
        self.assertEqual(self.daemon.config.cloudflared_loglevel, "debug")

    def test_new_run_parameters_restart_the_connector(self) -> None:
        tuned = config.load_config({**RESOLVED, "run_parameters": ["--region=us"]})
        self.app.apply_config(copy.deepcopy({**STORED, "run_parameters": ["--region=us"]}), tuned)
        self.assertIn("restart", self.daemon.calls)
        self.assertEqual(self.daemon.config.run_parameters, ("--region=us",))

    def test_panel_change_writes_references_and_applies_in_the_loop(self) -> None:
        self.app.panel_toggle("notify_on_failure", False)
        self.assertEqual(self.sup.written[-1], {**STORED, "notify_on_failure": False})
        self.assertTrue(self.app.config.notify_on_failure)  # not applied yet
        self.assertFalse(self.app.view_config().notify_on_failure)  # but the panel shows it
        self.assertEqual(self.daemon.calls, [])
        self.app._tick()
        self.assertFalse(self.app.config.notify_on_failure)
        self.assertNotIn("restart", self.daemon.calls)

    def test_panel_change_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("notify_on_failure", False)
        self.assertEqual(self.sup.written, [])
        self.assertIsNone(self.app.pending)

    def test_panel_restart_happens_in_the_loop_not_on_the_http_thread(self) -> None:
        result = self.app.panel_action("restart", {})
        self.assertIn("Restarting", result or "")
        self.assertEqual(self.daemon.calls, [])  # the HTTP thread never touches the process
        self.app._tick()
        self.assertIn("restart", self.daemon.calls)

    def test_unknown_toggle_and_action_are_refused(self) -> None:
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("other", True)
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("nope", {})

    # -- Edits from the Configuration tab ------------------------------------------------
    def test_live_edits_are_applied(self) -> None:
        self.sup.stored = {**STORED, "log_level": "debug", "run_parameters": ["--region=us"]}
        self.app.check_options()
        self.assertEqual(self.app.config.log_level, "debug")
        self.assertEqual(self.app.config.run_parameters, ("--region=us",))
        self.assertIn("restart", self.daemon.calls)
        self.assertEqual(self.app.raw_options["tunnel_token"], "!secret ct")

    def test_invalid_live_edits_keep_the_running_connector(self) -> None:
        self.sup.stored = {**STORED, "run_parameters": ["--logfile=/tmp/x"]}
        self.app.check_options()
        self.assertEqual(self.app.config.run_parameters, ())
        self.assertNotIn("restart", self.daemon.calls)
        self.assertEqual(self.app.raw_options["run_parameters"], ["--logfile=/tmp/x"])  # remembered, not retried
        self.sup.stored = {**STORED, "run_parameters": ["--region=us"]}
        self.app.check_options()
        self.assertEqual(self.app.config.run_parameters, ("--region=us",))

    def test_a_secret_added_after_start_keeps_the_running_connector(self) -> None:
        self.sup.stored = {**STORED, "tunnel_token": "!secret added-later"}
        self.app.check_options()
        self.assertEqual(self.app.config.tunnel_token, TOKEN)  # still the resolved start-up token
        self.assertNotIn("restart", self.daemon.calls)
        self.assertEqual(self.app.raw_options["tunnel_token"], "!secret added-later")

    # -- Failure notifications -----------------------------------------------------------
    def test_a_down_tunnel_is_notified_once_and_recovery_after(self) -> None:
        for _ in range(main.FAILURES_BEFORE_NOTIFY - 1):
            self.app.watch()
        self.assertEqual(self.sup.sent, [])
        self.app.watch()  # the poll that crosses the threshold
        self.assertEqual(len(self.sup.sent), 1)
        self.assertIn("down", self.sup.sent[0][1].lower())
        for _ in range(main.FAILURES_BEFORE_NOTIFY * 2):
            self.app.watch()
        self.assertEqual(len(self.sup.sent), 1)  # once per outage
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.app.watch()
        self.assertEqual(len(self.sup.sent), 2)
        self.assertIn("recover", self.sup.sent[1][1].lower())
        self.app.watch()
        self.assertEqual(len(self.sup.sent), 2)

    def test_no_notifications_when_switched_off(self) -> None:
        self.app.apply_config(
            copy.deepcopy({**STORED, "notify_on_failure": False}),
            config.load_config({**RESOLVED, "notify_on_failure": False}),
        )
        for _ in range(main.FAILURES_BEFORE_NOTIFY + 3):
            self.app.watch()
        self.assertEqual(self.sup.sent, [])

    # -- Phones ---------------------------------------------------------------------------
    def test_phones_are_found_and_switched_per_phone(self) -> None:
        self.sup.targets = ["mobile_app_pixel", "mobile_app_iphone"]
        self.app.refresh_phones()
        self.assertEqual(self.app.discovered, ["mobile_app_pixel", "mobile_app_iphone"])
        self.assertEqual(self.app.phones, {"mobile_app_pixel": True, "mobile_app_iphone": True})
        self.app.panel_toggle("phone:mobile_app_iphone", False)
        self.store.save()
        saved = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["phones"]["mobile_app_iphone"], False)
        self.sup.targets = None  # Home Assistant unreachable: keep the known phones
        self.app.refresh_phones()
        self.assertEqual(self.app.discovered, ["mobile_app_pixel", "mobile_app_iphone"])
        self.sup.targets = ["mobile_app_pixel"]  # a phone was unlinked: dropped from the switches
        self.app.refresh_phones()
        self.assertEqual(self.app.phones, {"mobile_app_pixel": True})
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("phone:mobile_app_iphone", True)

    def test_notifications_go_to_the_switched_on_phones_only(self) -> None:
        self.sup.targets = ["mobile_app_pixel", "mobile_app_iphone"]
        self.app.refresh_phones()
        self.app.panel_toggle("phone:mobile_app_iphone", False)
        for _ in range(main.FAILURES_BEFORE_NOTIFY):
            self.app.watch()
        self.assertEqual(len(self.sup.sent), 1)
        self.assertEqual(self.sup.sent[0][0], ["mobile_app_pixel"])

    # -- Startup and shutdown ---------------------------------------------------------------
    def test_startup_with_a_valid_token_returns_the_config(self) -> None:
        secrets = webui.SecretRefs()
        secrets.learn(STORED, RESOLVED)
        router = webui.PanelRouter()
        result = main._load_config(self.sup, secrets, router, threading.Event())
        self.assertIsNotNone(result)
        raw, cfg = result
        self.assertEqual(raw["tunnel_token"], "!secret ct")  # references, never values
        self.assertEqual(cfg.tunnel_token, TOKEN)

    def test_startup_with_an_empty_token_shows_setup_guidance(self) -> None:
        self.sup.stored = {"tunnel_token": ""}
        secrets = webui.SecretRefs()
        router = webui.PanelRouter()
        stop = threading.Event()
        stop.set()  # stop before the 15s poll interval ticks: the panel shows setup guidance meanwhile
        result = main._load_config(self.sup, secrets, router, stop)
        self.assertIsNone(result)  # stopped before a token appeared
        state = json.dumps(router.get_state())
        self.assertIn("tunnel_token", state)
        self.assertIn("Zero Trust", state)

    def test_run_starts_and_stops_the_connector(self) -> None:
        def stop_soon() -> None:
            self.app.stopping.set()
            self.app.wake.set()

        threading.Timer(0.2, stop_soon).start()
        self.app.run()
        self.assertEqual(self.daemon.calls[0], "start")
        self.assertEqual(self.daemon.calls[-1], "stop")


if __name__ == "__main__":
    unittest.main()
