"""Tests for the app: options with !secret references, restart decisions, failure notifications, panel actions (no network)."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import account as acc
import cloudflared as cf
import config
import ha_proxy
import main
import reach
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
        self.config_valid = True
        self.restarted = 0
        self.checks = 0

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

    def self_ip(self) -> str | None:
        return "172.30.33.5"

    def core_info(self) -> dict[str, object]:
        return {"port": 8123, "ssl": False}

    def check_core_config(self) -> tuple[bool, str]:
        self.checks += 1
        return self.config_valid, "" if self.config_valid else "Invalid config for [http]"

    def restart_core(self) -> None:
        self.restarted += 1


class FakeAccount:
    """Stands in for account.Account: records the cloudflared steps the app asks for."""

    def __init__(self) -> None:
        self.signed = False
        self.id: str | None = None
        self.zone = "example.com"
        self.route_result = acc.ROUTE_OK
        self.fail: str | None = None
        self.calls: list[object] = []
        self.ingress_text: str | None = None
        self.previous_text: str | None = None
        self.login = SimpleNamespace(
            state=acc.LOGIN_IDLE, url=None, error=None, start=lambda: self.calls.append("login")
        )

    def signed_in(self) -> bool:
        return self.signed

    def tunnel_id(self) -> str | None:
        return self.id

    def zone_name(self) -> str | None:
        return self.zone

    def create_tunnel(self) -> tuple[str, str]:
        self.calls.append("create")
        if self.fail:
            raise acc.AccountError(self.fail)
        self.id = "6ff42ae2-765d-4adf-8112-31c55c1551ef"
        return self.id, "home-assistant"

    def route_dns(self, tunnel_id: str, hostname: str, *, overwrite: bool = False) -> str:  # noqa: ARG002
        self.calls.append(("route", hostname, overwrite))
        return acc.ROUTE_OK if overwrite else self.route_result

    def write_ingress(
        self,
        tunnel_id: str,  # noqa: ARG002
        hostname: str,
        service: str,
        *,
        origin_request: dict[str, object] | None = None,
    ) -> bool:
        self.calls.append(("ingress", hostname, service, dict(origin_request or {})))
        text = repr((hostname, service, sorted((origin_request or {}).items())))
        if text == self.ingress_text:
            return False
        self.previous_text = self.ingress_text
        self.ingress_text = text
        return True

    def has_previous(self) -> bool:
        return self.previous_text is not None

    def restore_previous(self) -> bool:
        if self.previous_text is None:
            return False
        self.ingress_text, self.previous_text = self.previous_text, None
        self.calls.append("restore")
        return True

    def discard_previous(self) -> None:
        self.previous_text = None

    def sign_out(self) -> str | None:
        self.calls.append("sign_out")
        self.signed = False
        self.id = None
        return None


class FakeDaemon:
    """Stands in for cloudflared.Cloudflared: a status object plus recorded process control."""

    def __init__(self, cfg: config.Config) -> None:
        self.config = cfg
        self.tunnel_id: str | None = None
        self.status = cf.TunnelStatus()
        self.calls: list[str] = []

    @property
    def ready(self) -> bool:
        return self.config.mode == config.MODE_TOKEN or bool(self.tunnel_id)

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def restart(self) -> None:
        self.calls.append("restart")
        self.status.restarts += 1
        self.status.set(cf.STATE_STARTING)  # like the real connector: reconnects from scratch
        self.status.connections = 0

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
        self.account = FakeAccount()
        self.ha_config = self.dir / "configuration.yaml"
        self.ha_config.write_text("default_config:\n", encoding="utf-8")
        self.probed: list[str] = []
        self.probe_code: int | None = 401
        self.app = main.App(
            config.load_config(secrets.resolve(copy.deepcopy(STORED))),
            self.sup,
            self.store,
            copy.deepcopy(STORED),
            daemon=self.daemon,  # type: ignore[arg-type]
            account=self.account,  # type: ignore[arg-type]
            ha_config=self.ha_config,
            probe=self._probe,
            manager_status=self.dir / "ssl-cert-manager" / "status.json",
            status_file=self.dir / "cloudflare-tunnel" / "status.json",
            sleep=lambda _seconds: None,
        )
        self.app.secrets = secrets

    def _probe(self, hostname: str) -> reach.Result:
        self.probed.append(hostname)
        return reach.Result(self.probe_code, "" if self.probe_code else "connection refused by 192.0.2.1")

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

    def test_startup_with_an_empty_token_starts_in_login_mode(self) -> None:
        self.sup.stored = {"tunnel_token": ""}
        result = main._load_config(self.sup, webui.SecretRefs(), webui.PanelRouter(), threading.Event())
        self.assertIsNotNone(result)
        self.assertEqual(result[1].mode, config.MODE_LOGIN)

    def test_startup_with_a_bad_option_shows_setup_guidance(self) -> None:
        self.sup.stored = {"tunnel_token": "", "external_hostname": "https://nope"}
        router = webui.PanelRouter()
        stop = threading.Event()
        stop.set()  # stop before the 15s poll interval ticks: the panel shows setup guidance meanwhile
        self.assertIsNone(main._load_config(self.sup, webui.SecretRefs(), router, stop))
        self.assertIn("external_hostname", json.dumps(router.get_state()))

    # -- Login mode: sign in → hostname → tunnel + DNS ----------------------------------
    def _login_mode(self, **options: object) -> None:
        raw = {"tunnel_token": "", "notify_on_failure": True, "run_parameters": [], "log_level": "info", **options}
        self.sup.stored = copy.deepcopy(raw)
        self.app.apply_config(copy.deepcopy(raw), config.load_config(raw))
        self.daemon.calls.clear()

    def test_login_mode_starts_with_the_sign_in_button_and_no_outage_alerts(self) -> None:
        self._login_mode()
        text = self._state_text()
        self.assertIn("Sign in with Cloudflare", text)
        self.assertNotIn("Edge connections", text)  # the setup card replaces the connector card
        for _ in range(main.FAILURES_BEFORE_NOTIFY + 2):
            self.app._tick()
        self.assertEqual(self.sup.sent, [])  # not set up yet is not an outage

    def test_sign_in_runs_on_the_loop_thread_and_shows_the_link(self) -> None:
        self._login_mode()
        self.app.panel_action("login", {})
        self.assertEqual(self.account.calls, [])  # queued, not run on the HTTP thread
        self.app._tick()
        self.assertEqual(self.account.calls, ["login"])
        self.account.login.state = acc.LOGIN_WAITING
        self.account.login.url = "https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin"
        text = self._state_text()
        self.assertIn("Open Cloudflare sign-in", text)
        self.assertIn('"url": "https://dash.cloudflare.com/argotunnel', text)

    def test_after_sign_in_the_hostname_is_asked_and_the_tunnel_is_set_up(self) -> None:
        self._login_mode()
        self.account.signed = True
        self.app._tick()
        self.assertIn('"prompt_value": "home.example.com"', self._state_text())
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("hostname", {"text": "ha.other.org"})  # outside the signed-in zone
        self.app.panel_action("hostname", {"text": "Home.Example.com"})
        self.assertEqual(self.sup.written[-1]["external_hostname"], "home.example.com")
        self.app._tick()
        self.assertEqual(
            self.account.calls,
            [
                "create",
                ("route", "home.example.com", False),
                ("ingress", "home.example.com", "http://homeassistant:8123", {}),
            ],
        )
        self.assertEqual(self.daemon.tunnel_id, self.account.id)
        self.assertIn("restart", self.daemon.calls)
        self.assertEqual(self.app.tunnel_state["hostname"], "home.example.com")
        self.daemon.status.set(cf.STATE_CONNECTED)
        text = self._state_text()
        self.assertIn("Edge connections", text)  # set up: the connector card is back
        self.assertIn("https://home.example.com", text)
        self.app._tick()
        self.assertEqual(self.account.calls.count("create"), 1)  # done once

    def test_a_two_level_name_gets_a_one_level_suggestion(self) -> None:
        self.assertEqual(main.one_level_name("ha.int.example.com", "example.com"), "ha.example.com")
        self.assertIsNone(main.one_level_name("home.example.com", "example.com"))
        self.assertIsNone(main.one_level_name("example.com", "example.com"))
        self.assertIsNone(main.one_level_name("a.b.other.org", "example.com"))
        self._login_mode()
        self.account.signed = True
        self.app._tick()
        self.app.panel_action("hostname", {"text": "ha.int.example.com"})  # allowed: an Advanced Certificate covers it
        self.app._tick()
        self.daemon.status.set(cf.STATE_CONNECTED)
        text = self._state_text()
        self.assertIn("two levels below example.com", text)
        self.assertIn('"prompt_value": "ha.example.com"', text)
        self.app.reach = ("Reachable", "ok")  # e.g. with an Advanced Certificate: no nagging
        text = self._state_text()
        self.assertNotIn("two levels below", text)
        self.assertIn('"prompt_value": "ha.int.example.com"', text)

    def test_an_existing_dns_record_is_only_replaced_when_asked(self) -> None:
        self._login_mode(external_hostname="home.example.com")
        self.account.signed = True
        self.account.route_result = acc.ROUTE_EXISTS
        self.app._tick()
        self.assertTrue(self.app.dns_conflict)
        self.assertNotIn("restart", self.daemon.calls)
        self.assertIn("Replace DNS record", self._state_text())
        self.app._tick()
        self.assertEqual(sum(1 for c in self.account.calls if isinstance(c, tuple) and c[0] == "route"), 1)
        self.app.panel_action("overwrite_dns", {})
        self.app._tick()
        self.assertIn(("route", "home.example.com", True), self.account.calls)
        self.assertIn("restart", self.daemon.calls)
        self.assertFalse(self.app.dns_conflict)

    def test_a_failed_step_waits_for_try_again(self) -> None:
        self._login_mode(external_hostname="home.example.com")
        self.account.signed = True
        self.account.fail = "Could not create the tunnel: quota exceeded"
        self.app._tick()
        self.app._tick()
        self.assertEqual(self.account.calls.count("create"), 1)  # no retry loop
        self.assertIn("quota exceeded", self._state_text())
        self.account.fail = None
        self.app.panel_action("retry", {})
        self.app._tick()
        self.assertEqual(self.account.calls.count("create"), 2)
        self.assertIn("restart", self.daemon.calls)

    def test_sign_out_stops_and_forgets_the_tunnel(self) -> None:
        self._login_mode(external_hostname="home.example.com")
        self.account.signed = True
        self.app._tick()
        self.app.panel_action("sign_out", {})
        self.app._tick()
        self.assertIn("sign_out", self.account.calls)
        self.assertIsNone(self.daemon.tunnel_id)
        self.assertNotIn("hostname", self.app.tunnel_state)
        self.assertIn("Sign in with Cloudflare", self._state_text())

    def test_token_mode_refuses_the_sign_in_steps(self) -> None:
        for step in ("login", "sign_out", "overwrite_dns", "retry"):
            with self.assertRaises(webui.PanelError):
                self.app.panel_action(step, {})

    # -- Home Assistant proxy settings ---------------------------------------------------
    def test_the_proxy_settings_are_added_once_and_restart_waits_for_a_click(self) -> None:
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.app._tick()
        text = self.ha_config.read_text(encoding="utf-8")
        self.assertIn("use_x_forwarded_for: true", text)
        self.assertIn("- 172.30.33.0/24", text)
        self.assertTrue((self.dir / f"configuration.yaml{ha_proxy.BACKUP_SUFFIX}").exists())
        self.assertEqual(self.sup.restarted, 0)
        self.assertIn("Restart Home Assistant", self._state_text())
        self.app.panel_action("restart_ha", {})
        self.app._tick()
        self.assertEqual(self.sup.restarted, 1)
        self.assertNotIn("Restart Home Assistant", self._state_text())
        self.ha_config.write_text("default_config:\n", encoding="utf-8")  # the user removed it again
        self.app._tick()
        self.assertEqual(self.ha_config.read_text(encoding="utf-8"), "default_config:\n")  # once per install
        self.assertEqual(self.sup.checks, 1)

    def test_a_failed_config_check_restores_the_file(self) -> None:
        self.sup.config_valid = False
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.app._tick()
        self.assertEqual(self.ha_config.read_text(encoding="utf-8"), "default_config:\n")
        self.assertEqual(self.app.tunnel_state["proxy_status"], ha_proxy.STATUS_INVALID)
        self.assertIn("Check again", self._state_text())

    def test_nothing_is_edited_before_the_tunnel_connects(self) -> None:
        self.app._tick()
        self.assertEqual(self.ha_config.read_text(encoding="utf-8"), "default_config:\n")
        self.assertNotIn("proxy_status", self.app.tunnel_state)

    # -- Public URL check ------------------------------------------------------------------
    def test_the_public_url_is_checked_when_connected(self) -> None:
        self.app.apply_config(
            copy.deepcopy({**STORED, "external_hostname": "home.example.com"}),
            config.load_config({**RESOLVED, "external_hostname": "home.example.com"}),
        )
        self.app._check_reach()
        self.assertEqual(self.probed, [])  # not connected yet
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.app._check_reach()
        self.app._reach_thread.join(5)
        self.assertEqual(self.probed, ["home.example.com"])
        self.assertIn("Reachable", self._state_text())
        self.probe_code = 400
        self.app.panel_action("check_reach", {})
        self.app._tick()
        self.app._reach_thread.join(5)
        self.assertIn("HTTP 400", self._state_text())

    # -- Origin TLS and the check after every change ---------------------------------------
    def _connected(self, connections: int = 2) -> None:
        self.daemon.status.set(cf.STATE_CONNECTED)
        self.daemon.status.connections = connections

    def _finish_verify(self) -> None:
        self.app._tick()  # connected: the probe starts on its own thread
        if self.app._verify_thread is not None:
            self.app._verify_thread.join(5)
        self.app._tick()

    def _set_up_tunnel(self) -> None:
        self._login_mode(external_hostname="home.example.com")
        self.account.signed = True
        self.app._tick()
        self.assertEqual(self.app.verify["state"], main.VERIFY_PENDING)
        self._connected()
        self._finish_verify()
        self.assertEqual(self.app.verify["state"], main.VERIFY_OK)

    def _manager(self, **proxy: object) -> Path:
        bundle = self.dir / "ca-bundle.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
        status = {
            "version": 1,
            "trust_bundle": str(bundle),
            "default": {"id": "home", "names": ["home.example.com"]},
            "certificates": [{"id": "home", "names": ["home.example.com"]}],
            "proxy": {"enabled": True, "host": "a1b2c3d4-ssl-cert-manager", "port": 8443, **proxy},
            "ha_https": False,
        }
        path = self.dir / "ssl-cert-manager" / "status.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(status), encoding="utf-8")
        self.app.next_origin_check = 0.0
        return bundle

    def test_a_new_route_is_verified_and_published(self) -> None:
        self._set_up_tunnel()
        self.assertIn("Verified", str(self.app.verify["message"]))
        self.assertIn("2 edge connections", str(self.app.verify["message"]))
        published = json.loads((self.dir / "cloudflare-tunnel" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(published["hostname"], "home.example.com")
        self.assertEqual(published["verify"]["state"], "ok")
        self.assertIn("Tunnel check passed", self._state_text())

    def test_the_route_follows_ssl_cert_manager_and_is_verified(self) -> None:
        self._set_up_tunnel()
        bundle = self._manager()
        self.app._tick()
        self._connected()
        self.assertEqual(
            self.account.calls[-1],
            (
                "ingress",
                "home.example.com",
                "https://a1b2c3d4-ssl-cert-manager:8443",
                {"caPool": str(bundle), "originServerName": "home.example.com"},
            ),
        )
        self.assertTrue(self.app._verify_rollback)
        self._finish_verify()
        self.assertEqual(self.app.verify["state"], main.VERIFY_OK)
        self.assertFalse(self.account.has_previous())  # verified: the old file is gone
        self.assertIn("certificate checked for home.example.com", self._state_text())

    def test_a_failing_change_is_rolled_back_and_not_retried(self) -> None:
        self._set_up_tunnel()
        self._manager()
        self.app._tick()  # re-routed to the proxy, check pending
        self.probe_code = 502
        self._connected()
        self.app._tick()  # connected: probe
        self.app._verify_thread.join(5)
        self.daemon.status.origin_error = "x509: certificate signed by unknown authority"
        self.app._tick()  # failed: rolled back, checking the old route
        self.assertIn("restore", self.account.calls)
        self.assertEqual(self.sup.sent[-1][1], "Cloudflare Tunnel change rolled back")
        self.assertIn("unknown authority", self.sup.sent[-1][2])
        self.assertEqual(self.app.origin.service, "http://homeassistant:8123")  # type: ignore[union-attr]
        self.probe_code = 401
        self._connected()
        self._finish_verify()
        self.assertEqual(self.app.verify["state"], main.VERIFY_ROLLED_BACK)
        self.assertIn("works again", str(self.app.verify["message"]))
        self.assertIn("Change rolled back", self._state_text())
        # The same route is not tried again...
        ingress_calls = sum(1 for c in self.account.calls if isinstance(c, tuple) and c[0] == "ingress")
        self.app.next_origin_check = 0.0
        self.app._tick()
        self.assertEqual(
            sum(1 for c in self.account.calls if isinstance(c, tuple) and c[0] == "ingress"), ingress_calls
        )
        # ...until SSL Cert Manager changes something.
        self._manager(port=8444)
        self.app._tick()
        self.assertEqual(self.account.calls[-1][2], "https://a1b2c3d4-ssl-cert-manager:8444")  # type: ignore[index]

    def test_verify_mode_reports_but_never_rolls_back(self) -> None:
        self._set_up_tunnel()
        self.sup.stored = {**self.sup.stored, "origin_tls": "verify"}
        self.app.check_options()
        self._manager()
        self.app._tick()
        self.assertFalse(self.app._verify_rollback)
        self.probe_code = 526
        self._connected()
        self._finish_verify()
        self.assertEqual(self.app.verify["state"], main.VERIFY_FAILED)
        self.assertNotIn("restore", self.account.calls)
        self.assertEqual(self.sup.sent[-1][1], "Cloudflare Tunnel check failed")
        self.assertIn("HTTP 526", self.sup.sent[-1][2])

    def test_a_connector_that_does_not_reconnect_fails_the_check(self) -> None:
        self._set_up_tunnel()
        self.daemon.status.set(cf.STATE_STOPPED)
        self.app._begin_verify("a test", rollback=False)
        self.app._verify_deadline = 0.0
        self.app._tick()
        self.assertEqual(self.app.verify["state"], main.VERIFY_FAILED)
        self.assertIn("did not reconnect", str(self.app.verify["message"]))

    def test_check_now_changes_nothing(self) -> None:
        self._set_up_tunnel()
        calls = list(self.account.calls)
        self.app.panel_action("check_reach", {})
        self.app._tick()
        self.assertEqual(self.app.verify["state"], main.VERIFY_PENDING)
        self._finish_verify()
        self.assertEqual(self.app.verify["state"], main.VERIFY_OK)
        self.assertEqual(self.account.calls, calls)

    def test_token_mode_shows_where_to_set_the_ca_pool(self) -> None:
        bundle = self._manager()
        self.assertIn(f"set CA Pool to {bundle}".replace("\\", "\\\\"), self._state_text())

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
