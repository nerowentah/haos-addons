#!/usr/bin/env python3
"""Home Assistant app: Cloudflare Tunnel - signs in to Cloudflare (or takes a Zero Trust token) and runs cloudflared."""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import account as acc
import cloudflared as cf
import ha_proxy
from config import (
    MODE_LOGIN,
    Config,
    ConfigError,
    apply_log_level,
    load_config,
    load_options,
    parse_hostname,
)
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import (
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    action,
    badge,
    external,
    initial_options,
    notice,
    row,
    stat,
    toggle,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger("cloudflare_tunnel")

VERSION = os.environ.get("CF_TUNNEL_VERSION", "dev")
LOOP_SECONDS = 5  # connector status poll cadence; the panel refreshes itself every 5s too
MIN_SLEEP_SECONDS = 1
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
STORE_SAVE_SECONDS = 60
FAILURES_BEFORE_NOTIFY = 6  # polls: ~30s of not being connected before the phones are told
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "cloudflare_tunnel"
REACH_CHECK_SECONDS = 600  # how often the public hostname is tried from outside
REACH_FIRST_CHECK_SECONDS = 20  # after a (re)connect: give DNS and the edge a moment
DEFAULT_CORE_PORT = 8123

STATE_BADGE = {
    cf.STATE_SETUP: ("Not set up", "neutral"),
    cf.STATE_STARTING: ("Starting", "neutral"),
    cf.STATE_CONNECTED: ("Connected", "ok"),
    cf.STATE_DEGRADED: ("Degraded", "warn"),
    cf.STATE_STOPPED: ("Restarting", "warn"),
    cf.STATE_ERROR: ("Error", "error"),
}
STATE_ICON = {
    cf.STATE_SETUP: "mdi:cloud-outline",
    cf.STATE_STARTING: "mdi:cloud-outline",
    cf.STATE_CONNECTED: "mdi:cloud-check-outline",
    cf.STATE_DEGRADED: "mdi:cloud-outline",
    cf.STATE_STOPPED: "mdi:cloud-off-outline",
    cf.STATE_ERROR: "mdi:cloud-alert",
}


def _ago(moment: datetime | None, now: datetime) -> str | None:
    """A short "x ago" label for the panel."""

    if moment is None:
        return None
    seconds = max(0, int((now - moment).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def http_status(url: str) -> int | None:
    """The HTTP status of a GET without credentials, or None when nothing answers."""

    request = urllib.request.Request(url, headers={"User-Agent": "cloudflare-tunnel-app"})  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - https only
            return int(response.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def describe_reach(code: int | None) -> tuple[str, str]:
    """(text, tone) for the public URL check."""

    if code in (200, 401):  # 401: Home Assistant itself answered and wants a login
        return "Reachable", "ok"
    if code == 400:  # noqa: PLR2004 - HTTP status
        return "Home Assistant rejects the proxy (HTTP 400): see Home Assistant proxy settings", "error"
    if code is None:
        return "No answer from the public hostname", "warn"
    if code >= 500:  # noqa: PLR2004 - HTTP status
        return f"Cloudflare cannot reach Home Assistant (HTTP {code})", "error"
    return f"HTTP {code}", "warn"


class App:
    """Runs the cloudflared connector, serves the panel and notifies when the tunnel fails."""

    def __init__(  # noqa: PLR0913 - the tests inject the seams
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        *,
        daemon: cf.Cloudflared | None = None,
        account: acc.Account | None = None,
        ha_config: Path = ha_proxy.CONFIG_PATH,
        probe: Callable[[str], int | None] = http_status,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.supervisor = supervisor
        self.store = store
        self.daemon = daemon or cf.Cloudflared(config)
        self.version = VERSION
        self.discovered: list[str] = []
        self.down_polls = 0
        self.down_notified = False
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.next_phone_check = 0.0
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.restart_requested = False
        # Login mode: the panel queues steps; the loop thread runs them (cloudflared calls block).
        self.account = account or acc.Account()
        self.ha_config = ha_config
        self.probe = probe
        self.steps: list[str] = []
        self.setup_error: str | None = None
        self.setup_note: str | None = None
        self.dns_conflict = False
        self.reach: tuple[str, str] | None = None
        self.next_reach_check = 0.0
        self._reach_thread: threading.Thread | None = None
        if config.mode == MODE_LOGIN:
            self.daemon.tunnel_id = self._routed_tunnel_id()

    @property
    def tunnel_state(self) -> dict[str, object]:
        """What the app set up (kept in /data): routed hostname and the proxy-settings outcome."""

        return self.store.section("tunnel")

    def _routed_tunnel_id(self) -> str | None:
        """The created tunnel, once a hostname has been routed to it."""

        return self.account.tunnel_id() if self.tunnel_state.get("hostname") else None

    def _set_tunnel(self, **values: object) -> None:
        for key, value in values.items():
            if value is None:
                self.tunnel_state.pop(key, None)
            else:
                self.tunnel_state[key] = value
        self.store.dirty = True

    # -- Options (panel + Configuration tab, applied without a restart) -------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread); restarts the connector when they matter."""

        old = self.config
        self.config = config
        self.raw_options = raw
        self.daemon.config = config
        self.daemon.tunnel_id = self._routed_tunnel_id() if config.mode == MODE_LOGIN else None
        if old.external_hostname != config.external_hostname:
            # Login mode routes the new name in the setup pass; both modes re-check the public URL.
            self.dns_conflict = False
            self.setup_error = None
            self.reach = None
            self.next_reach_check = 0.0
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)  # before applying: may hide info
            apply_log_level(config.log_level)
        if old.daemon_spec() != config.daemon_spec():
            _LOGGER.info("Tunnel options changed; restarting the connector")
            self.daemon.restart()
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        """Pick up edits made in the Configuration tab."""

        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- Phones (notification targets for this app) ---------------------------------
    @property
    def phones(self) -> dict[str, object]:
        """Per-phone switches kept in /data (phone name -> notify)."""

        return self.store.section("phones")

    def refresh_phones(self) -> None:
        """Re-list the phones linked to Home Assistant (new phones are notified by default)."""

        found = self.supervisor.discover_mobile_targets()
        if found is None:
            return
        with self.options_lock:
            merged = merge_discovered(self.phones, found)
            changed = merged != self.phones or found != self.discovered
            self.discovered = found
            if merged != self.phones:
                self.store.data["phones"] = merged
                self.store.dirty = True
        if changed:
            targets = effective_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, title: str, message: str, suffix: str = "") -> None:
        """Notify the switched-on phones (or a persistent notification) when enabled."""

        if self.config.notify_on_failure:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- Failure notifications ------------------------------------------------------
    def watch(self) -> None:
        """Tell the phones once per outage when the tunnel is not connected (and when it recovers)."""

        if self.daemon.status.state == cf.STATE_CONNECTED:
            if self.down_notified:
                self.down_notified = False
                self.down_polls = 0
                self.notify("Cloudflare Tunnel recovered", "The tunnel is connected again.", "_down")
            else:
                self.down_polls = 0
            return
        self.down_polls += 1
        if self.down_polls >= FAILURES_BEFORE_NOTIFY and not self.down_notified:
            self.down_notified = True
            message = self.daemon.status.last_error or "The connector is not connected to Cloudflare's edge."
            self.notify("Cloudflare Tunnel is down", message, "_down")

    # -- Login-mode setup (loop thread) -----------------------------------------------
    def _queue(self, step: str) -> None:
        with self.options_lock:
            self.steps.append(step)
        self.wake.set()

    def _local_service(self) -> tuple[str, bool]:
        """Where the tunnel sends traffic: Home Assistant on the internal network (and whether it uses SSL)."""

        try:
            info = self.supervisor.core_info()
            port = int(str(info.get("port") or DEFAULT_CORE_PORT))
            ssl = bool(info.get("ssl"))
        except (SupervisorError, ValueError):
            port, ssl = DEFAULT_CORE_PORT, False
        return f"{'https' if ssl else 'http'}://homeassistant:{port}", ssl

    def _provision(self, *, overwrite: bool = False) -> None:
        """Create the tunnel (once), route the hostname to it, write the ingress and (re)start the connector."""

        hostname = self.config.external_hostname
        try:
            tunnel_id = self.account.tunnel_id()
            if not tunnel_id:
                tunnel_id, _name = self.account.create_tunnel()
            if self.account.route_dns(tunnel_id, hostname, overwrite=overwrite) == acc.ROUTE_EXISTS:
                self.dns_conflict = True
                _LOGGER.warning("%s already has a DNS record; replace it or choose another name in the panel", hostname)
                return
            service, ssl = self._local_service()
            self.account.write_ingress(tunnel_id, hostname, service, no_tls_verify=ssl)
        except acc.AccountError as err:
            self.setup_error = str(err)
            _LOGGER.error("Setup: %s", err)  # noqa: TRY400 - the message says it all
            return
        except OSError as err:
            self.setup_error = f"Could not write the tunnel files: {err}"
            _LOGGER.error("Setup: %s", self.setup_error)  # noqa: TRY400 - the message says it all
            return
        self.dns_conflict = False
        self.setup_error = None
        self._set_tunnel(hostname=hostname)
        self.daemon.tunnel_id = tunnel_id
        self.daemon.restart()
        self.next_reach_check = time.monotonic() + REACH_FIRST_CHECK_SECONDS

    def _sign_out(self) -> None:
        self.daemon.stop()
        self.setup_note = self.account.sign_out()
        self.daemon.tunnel_id = None
        self._set_tunnel(hostname=None)
        self.dns_conflict = False
        self.setup_error = None
        self.reach = None
        self.daemon.start()  # nothing to run: shows "Not set up"

    def _restart_home_assistant(self) -> None:
        try:
            self.supervisor.restart_core()
        except SupervisorError as err:
            self._set_tunnel(proxy_message=f"Home Assistant did not restart: {err}")
            return
        _LOGGER.info("Restarting Home Assistant to apply the proxy settings")
        self._set_tunnel(restart_pending=None)
        self.next_reach_check = time.monotonic() + 120  # check again once it is back

    def _run_step(self, step: str) -> None:
        if step == "login":
            self.setup_note = None
            self.account.login.start()
        elif step == "retry":
            self.setup_error = None
        elif step == "overwrite_dns":
            self.dns_conflict = False
            self._provision(overwrite=True)
        elif step == "sign_out":
            self._sign_out()
        elif step == "restart_ha":
            self._restart_home_assistant()
        elif step == "fix_proxy":
            self._set_tunnel(proxy_status=None, proxy_message=None, restart_pending=None)
        elif step == "check_reach":
            self.next_reach_check = 0.0

    def _fix_proxy(self) -> None:
        """Once per install, when the tunnel first connects: make Home Assistant trust it."""

        if "proxy_status" in self.tunnel_state or self.daemon.status.state != cf.STATE_CONNECTED:
            return
        try:
            ip = self.supervisor.self_ip()
        except SupervisorError:
            ip = None
        result = ha_proxy.apply(self.supervisor, ha_proxy.network_for(ip), self.ha_config)
        if result.status != ha_proxy.STATUS_PRESENT:
            _LOGGER.info("Home Assistant proxy settings: %s", result.message)
        self._set_tunnel(
            proxy_status=result.status,
            proxy_message=result.message,
            restart_pending=True if result.status == ha_proxy.STATUS_ADDED else None,
        )

    def _setup(self) -> None:
        """Run queued panel steps, then whatever the current state needs next."""

        with self.options_lock:
            steps, self.steps = self.steps, []
        for step in steps:
            self._run_step(step)
        if self.config.mode == MODE_LOGIN and self.account.signed_in():
            self.account.zone_name()  # once: pre-fills the hostname prompt
            hostname = self.config.external_hostname
            routed = self.tunnel_state.get("hostname") == hostname and self.daemon.tunnel_id
            if hostname and not routed and not self.dns_conflict and not self.setup_error:
                self._provision()
        if self.daemon.ready:
            self._fix_proxy()

    def _check_reach(self) -> None:
        """Try the public hostname from outside, on a worker thread (it can take seconds)."""

        hostname = self.config.external_hostname
        if not hostname or self.daemon.status.state != cf.STATE_CONNECTED:
            return
        if time.monotonic() < self.next_reach_check:
            return
        if self._reach_thread is not None and self._reach_thread.is_alive():
            return
        self.next_reach_check = time.monotonic() + REACH_CHECK_SECONDS

        def check() -> None:
            self.reach = describe_reach(self.probe(f"https://{hostname}/api/"))

        self._reach_thread = threading.Thread(target=check, daemon=True, name="reach-check")
        self._reach_thread.start()

    # -- Panel callbacks (HTTP thread) ---------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if toggle_id == "notify_on_failure":
            self.update_options(lambda raw: raw.__setitem__("notify_on_failure", value))
            return
        raise PanelError("Unknown setting")

    def _set_hostname(self, text: object) -> str:
        try:
            hostname = parse_hostname(text)
        except ConfigError as err:
            raise PanelError(str(err)) from err
        if not hostname:
            raise PanelError("Enter a hostname, e.g. home.example.com")
        zone = self.account.zone
        if self.config.mode == MODE_LOGIN and zone and hostname != zone and not hostname.endswith(f".{zone}"):
            raise PanelError(f"Cloudflare sign-in covers {zone}: use a name under it, e.g. home.{zone}")
        self.update_options(lambda raw: raw.__setitem__("external_hostname", hostname))
        return f"Setting up {hostname}..."

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button (slow work is queued for the loop thread)."""

        if action_id == "restart":
            self.restart_requested = True  # the loop restarts the connector on its own thread
            self.wake.set()
            return "Restarting the connector..."
        if action_id == "hostname":
            return self._set_hostname(args.get("text"))
        login_only = {"login", "retry", "overwrite_dns", "sign_out"}
        if action_id in login_only and self.config.mode != MODE_LOGIN:
            raise PanelError("This app uses a Zero Trust token; clear tunnel_token in the Configuration tab to sign in")
        replies = {
            "login": "Starting the Cloudflare sign-in...",
            "retry": "Trying again...",
            "overwrite_dns": "Replacing the DNS record...",
            "sign_out": "Signing out...",
            "restart_ha": "Restarting Home Assistant...",
            "fix_proxy": "Checking the Home Assistant proxy settings...",
            "check_reach": "Checking the public URL...",
        }
        if action_id not in replies:
            raise PanelError("Unknown action")
        if action_id == "login" and self.account.signed_in():
            raise PanelError("Already signed in to Cloudflare")
        self._queue(action_id)
        return replies[action_id]

    # -- Panel view model ------------------------------------------------------------------
    def _hostname_action(self, config: Config, label: str = "Choose hostname") -> dict[str, object]:
        zone = self.account.zone
        prefill = config.external_hostname or (f"home.{zone}" if zone else "")
        return action(
            "hostname",
            label,
            prompt="Public hostname for Home Assistant (e.g. home.example.com)",
            prompt_value=prefill,
            icon="mdi:web",
        )

    def _sign_out_action(self) -> dict[str, object]:
        return action(
            "sign_out",
            "Sign out",
            confirm="Stop the tunnel, delete it at Cloudflare and forget the sign-in? "
            "The DNS record stays; delete it in the Cloudflare dashboard if you no longer need it.",
            danger=True,
            icon="mdi:logout",
        )

    def _setup_card(self, config: Config) -> dict[str, object] | None:  # noqa: PLR0912 - one branch per setup stage
        """Login mode until the tunnel runs: sign in → hostname → tunnel + DNS. None once set up."""

        if config.mode != MODE_LOGIN:
            return None
        signed_in = self.account.signed_in()
        routed = self.tunnel_state.get("hostname")
        hostname = config.external_hostname
        if signed_in and hostname and routed == hostname and self.daemon.ready:
            return None
        login = self.account.login
        actions: list[dict[str, object]] = []
        note: dict[str, str] | None = None
        if not signed_in:
            if login.state == acc.LOGIN_WAITING and login.url:
                chip = badge("Waiting for sign-in", "warn")
                actions.append(external("Open Cloudflare sign-in", login.url, button="Open Cloudflare sign-in"))
                note = notice(
                    "Cloudflare opens in a new tab: pick the domain for Home Assistant and click Authorize. "
                    "This page continues by itself.",
                    "neutral",
                )
            elif login.state == acc.LOGIN_STARTING:
                chip = badge("Starting sign-in...")
            else:
                chip = badge("Not set up")
                actions.append(action("login", "Sign in with Cloudflare", icon="mdi:login"))
                if login.state == acc.LOGIN_FAILED and login.error:
                    note = notice(login.error, "error")
        elif self.dns_conflict:
            chip = badge("DNS record exists", "warn")
            note = notice(
                f"{hostname} already has a DNS record in Cloudflare. Replace it so it points to this tunnel, "
                "or choose another name.",
                "warn",
            )
            actions.append(
                action(
                    "overwrite_dns",
                    "Replace DNS record",
                    confirm=f"Replace the existing DNS record of {hostname} so it points to this tunnel?",
                    danger=True,
                    icon="mdi:dns",
                )
            )
            actions.append(self._hostname_action(config, "Choose another name"))
        elif not hostname:
            chip = badge("Choose a hostname", "warn")
            actions.append(self._hostname_action(config))
        elif self.setup_error:
            chip = badge("Setup failed", "error")
            note = notice(self.setup_error, "error")
            actions += [
                action("retry", "Try again", icon="mdi:refresh"),
                self._hostname_action(config, "Change hostname"),
            ]
        else:
            chip = badge("Setting up...")
        if signed_in:
            actions.append(self._sign_out_action())
        if note is None and self.setup_note:
            note = notice(self.setup_note, "warn")
        done, todo = "mdi:check-circle", "mdi:progress-clock"
        card: dict[str, object] = {
            "title": "Set up",
            "icon": "mdi:cloud-lock",
            "subtitle": "Sign in once: the app creates the tunnel, its DNS record and the route to Home Assistant",
            "badge": chip,
            "links": [
                row(
                    "Sign in to Cloudflare",
                    "Done" if signed_in else "Pick the domain to use",
                    icon=done if signed_in else "mdi:login",
                ),
                row("Public hostname", hostname or "Not chosen yet", icon=done if hostname else todo),
                row(
                    "Tunnel and DNS record",
                    "Created" if routed else "Created automatically",
                    icon=done if routed else todo,
                ),
            ],
            "actions": actions,
        }
        if note:
            card["notice"] = note
        return card

    def _proxy_card(self) -> dict[str, object] | None:
        """Home Assistant's http: trusted_proxies outcome, while there is something to do about it."""

        status = self.tunnel_state.get("proxy_status")
        message = str(self.tunnel_state.get("proxy_message") or "")
        pending = bool(self.tunnel_state.get("restart_pending"))
        if status is None or (status == ha_proxy.STATUS_PRESENT) or (status == ha_proxy.STATUS_ADDED and not pending):
            return None
        card: dict[str, object] = {
            "title": "Home Assistant proxy settings",
            "icon": "mdi:shield-check",
            "subtitle": "Home Assistant must trust the tunnel, or it answers every request with HTTP 400",
        }
        if status == ha_proxy.STATUS_ADDED:
            card["notice"] = notice(message, "ok")
            card["actions"] = [
                action(
                    "restart_ha",
                    "Restart Home Assistant",
                    confirm="Restart Home Assistant now? It is unavailable for a minute or two.",
                    icon="mdi:restart",
                )
            ]
        else:
            card["notice"] = notice(message, "warn" if status == ha_proxy.STATUS_MANUAL else "error")
            card["actions"] = [action("fix_proxy", "Check again", icon="mdi:refresh")]
        return card

    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        config = self.view_config()
        status = self.daemon.status
        now = datetime.now(UTC)
        state = status.state
        text, tone = STATE_BADGE.get(state, ("Unknown", "warn"))
        login_mode = config.mode == MODE_LOGIN
        actions = [
            action("restart", "Restart connector", confirm="Restart the tunnel connector now?", icon="mdi:restart")
        ]
        if config.external_hostname:
            actions.append(action("check_reach", "Check public URL", icon="mdi:refresh"))
        if login_mode:
            actions += [self._hostname_action(config, "Change hostname"), self._sign_out_action()]
        tunnel: dict[str, object] = {
            "title": "Connector",
            "icon": STATE_ICON.get(state, "mdi:cloud-outline"),
            "subtitle": "Tunnel created by this app" if login_mode else "Tunnel managed in Cloudflare Zero Trust",
            "badge": badge(text, tone),
            "stats": [
                stat("Edge connections", status.connections),
                stat("Restarts", status.restarts),
                stat("Started", _ago(status.started_at, now) or "-"),
                stat("Last change", _ago(status.last_change, now)),
            ],
            "actions": actions,
        }
        if config.external_hostname:
            reach_text, _reach_tone = self.reach or ("Not checked yet", "neutral")
            tunnel["links"] = [
                external(
                    f"https://{config.external_hostname}",
                    f"https://{config.external_hostname}",
                    reach_text,
                    icon="mdi:web",
                )
            ]
        if status.last_error or state in (cf.STATE_ERROR, cf.STATE_DEGRADED):
            tunnel["notice"] = notice(
                status.last_error or "The connector is running but has no edge connection.",
                "error" if state == cf.STATE_ERROR else "warn",
            )
        elif self.reach and self.reach[1] == "error":
            tunnel["notice"] = notice(self.reach[0], "error")
        settings: dict[str, object] = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "Advanced flags (and a Zero Trust token, if you use one instead of signing in) "
            "live in the Configuration tab",
            "stats": [
                stat("Setup", "Signed in to Cloudflare" if login_mode else "Zero Trust token"),
                stat("Log level", config.log_level),
                stat("Run parameters", len(config.run_parameters)),
            ],
            "toggles": [
                toggle(
                    "notify_on_failure",
                    "Notifications",
                    config.notify_on_failure,
                    "Notify the phones below when the tunnel has been down for about 30 seconds, and when it recovers",
                    icon="mdi:bell",
                ),
            ],
        }
        if config.run_parameters:
            settings["links"] = [row(item, "Run parameter") for item in config.run_parameters]
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_failure,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        setup = self._setup_card(config)
        tunnel_cards = [card for card in (setup or tunnel, self._proxy_card()) if card]
        sections: list[dict[str, object]] = [
            {"title": "Tunnel", "icon": "mdi:cloud-lock", "cards": tunnel_cards},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]},
        ]
        return {
            "title": "Cloudflare Tunnel",
            "subtitle": f"Version {self.version} · cloudflared {status.version or 'starting'}",
            "sections": sections,
        }

    # -- Loop ----------------------------------------------------------------------------------
    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        if self.restart_requested:
            self.restart_requested = False
            self.daemon.restart()
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        self._setup()
        self.daemon.poll()
        if self.daemon.ready:
            self.watch()  # not set up yet is not an outage
        self._check_reach()

    def run(self) -> None:
        """Start the connector and run the loop until stopped."""

        self.daemon.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS or self.store.dirty:
                    self.store.save()
                    last_save = time.monotonic()
                deadline = min(self.next_options_check, self.next_phone_check)
                self.wake.wait(min(max(deadline - time.monotonic(), MIN_SLEEP_SECONDS), LOOP_SECONDS))
        finally:
            self.daemon.stop()
            self.store.save()
            _LOGGER.info("Stopped")


SETUP_HELP = (
    "Open Settings → Apps → Cloudflare Tunnel → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """# Leave tunnel_token empty to sign in to Cloudflare from this panel,
# or paste the token of a tunnel made in Zero Trust:
tunnel_token: ""
external_hostname: home.example.com"""


def _load_config(
    supervisor: Supervisor, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    """The stored options (!secret references kept) and their config; waits in the panel while invalid."""

    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve(raw))  # type: ignore[arg-type]

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        _LOGGER.info("Waiting for a valid configuration; open the Cloudflare Tunnel panel for details")
        view = SetupView("Cloudflare Tunnel", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(supervisor.fetch_options, validate, view, stop)
        if result is None:
            return None
        _LOGGER.info("Configuration is valid now; starting")
        return result  # type: ignore[return-value]


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting Cloudflare Tunnel %s", VERSION)
    version = cf.binary_version()
    if version:
        _LOGGER.info("cloudflared %s", version)

    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)
    supervisor = Supervisor(token)

    stop = threading.Event()
    router = PanelRouter()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        stop.set()
        if isinstance(router.target, App):
            router.target.stopping.set()
            router.target.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    server = PanelServer(WEB_DIR, router.get_state, router.on_toggle, router.on_action)
    server.start()
    try:
        secrets = SecretRefs()
        loaded = _load_config(supervisor, secrets, router, stop)
        if loaded is None:
            return
        raw_options, config = loaded
        if config.log_level != "info":
            _LOGGER.info("Log level set to %s", config.log_level)
        apply_log_level(config.log_level)
        store = Store()
        store.load()
        app = App(config, supervisor, store, raw_options)
        app.secrets = secrets
        if version:
            app.daemon.status.version = version
        router.target = app
        if stop.is_set():
            return
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
