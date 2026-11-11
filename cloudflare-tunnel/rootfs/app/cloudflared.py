"""Run and watch the cloudflared tunnel connector.

The token is passed to cloudflared through the ``TUNNEL_TOKEN`` environment
variable (documented in Cloudflare's "Tunnel run parameters"): it never
appears in the command line, in the app log or in the panel.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import IO

import config as app_config

_LOGGER = logging.getLogger(__name__)

CLOUDFLARED = "/usr/bin/cloudflared"
METRICS_ADDRESS = "127.0.0.1:36500"  # localhost only; never exposed to the network
TOKEN_ENV = "TUNNEL_TOKEN"  # noqa: S105 - env var name, not a secret
STOP_TIMEOUT_SECONDS = 20
# Restart backoff after repeated crashes: 1s, 2s, 5s, 10s, 30s, then every 60s.
BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60)
# A bad token keeps failing instantly: retry it slowly instead of crash-looping.
BAD_TOKEN_RETRY_SECONDS = 300

STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_DEGRADED = "degraded"  # running, but every edge connection is gone
STATE_STOPPED = "stopped"  # exited; waiting to restart
STATE_ERROR = "error"  # rejected token or missing binary; slow retry

_VERSION_RE = re.compile(r"cloudflared version ([0-9A-Za-z.\-]+)", re.IGNORECASE)
_REGISTERED_RE = re.compile(r"Registered tunnel connection\b")
_UNREGISTERED_RE = re.compile(r"(Unregistered tunnel connection|Connection terminated)\b")
_BAD_TOKEN_RE = re.compile(r"(Provided Tunnel token is not valid|invalid tunnel secret)", re.IGNORECASE)
_METRIC_RE = re.compile(r"^cloudflared_tunnel_ha_connections\s+(\d+)\b", re.MULTILINE)

SpawnFn = Callable[[list[str], dict[str, str]], "subprocess.Popen[bytes]"]
MetricsFn = Callable[[], "str | None"]


def _spawn(argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:  # type: ignore[type-var]
    """Start a process with its output piped to us."""

    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603 - argv is built in this file, never from user input


def _metrics_page() -> str | None:
    """The connector's Prometheus metrics page, or None when it does not answer.

    The page listens on localhost inside the container only; no port is ever
    published to the network.
    """

    try:
        with urllib.request.urlopen(f"http://{METRICS_ADDRESS}/metrics", timeout=5) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def _ha_connections(metrics_text: str | None) -> int | None:
    """The edge-connection gauge from a metrics page (None when there is no page or gauge)."""

    if metrics_text is None:
        return None
    match = _METRIC_RE.search(metrics_text)
    return int(match.group(1)) if match else None


def binary_version(run: SpawnFn = _spawn) -> str | None:
    """The cloudflared version banner (``cloudflared --version``), or None."""

    try:
        process = run([CLOUDFLARED, "--version"], dict(os.environ))
    except OSError:
        return None
    try:
        output = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
    finally:
        process.wait()
    match = _VERSION_RE.search(output)
    return match.group(1) if match else None


class _Reader(threading.Thread):
    """Reads cloudflared's output line by line; blocking reads live here, not in the app loop."""

    def __init__(self, output: IO[bytes], on_line: Callable[[str], None]) -> None:
        super().__init__(daemon=True, name="cloudflared-log")
        self._output = output
        self._on_line = on_line

    def run(self) -> None:
        try:
            for raw in self._output:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip():
                    self._on_line(line)
        except (OSError, ValueError):
            pass  # the pipe was closed while stopping: just exit


class TunnelStatus:
    """What the panel shows: live state, counters and the last error."""

    def __init__(self) -> None:
        self.state = STATE_STARTING
        self.connections = 0
        self.version: str | None = None
        self.started_at: datetime | None = None
        self.restarts = 0
        self.last_error: str | None = None
        self.last_change = datetime.now(UTC)

    def set(self, state: str, error: str | None = None) -> bool:
        """Move to ``state``; returns True when the state changed."""

        changed = state != self.state
        if changed:
            self.state = state
            self.last_change = datetime.now(UTC)
        if error is not None and error != self.last_error:
            self.last_error = error
            changed = True
        return changed

    def as_dict(self) -> dict[str, object]:
        """A plain copy for the panel thread to read safely."""

        return {
            "state": self.state,
            "connections": self.connections,
            "version": self.version,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "last_change": self.last_change.isoformat(),
        }


class Cloudflared:
    """Supervises one cloudflared process; call :meth:`poll` from the app loop."""

    def __init__(
        self,
        cfg: app_config.Config,
        spawn: SpawnFn = _spawn,
        clock: Callable[[], float] = time.monotonic,
        metrics: MetricsFn = _metrics_page,
    ) -> None:
        self.config = cfg
        self.status = TunnelStatus()
        self._spawn = spawn
        self._clock = clock
        self._metrics = metrics
        self._process: subprocess.Popen[bytes] | None = None  # type: ignore[type-var]
        self._lock = threading.Lock()
        self._conn_count = 0
        self._ever_connected = False
        self._bad_token = False
        self._backoff_index = 0
        self._restart_at = 0.0

    # -- Process lifecycle -----------------------------------------------------
    def _argv(self) -> list[str]:
        return [
            CLOUDFLARED,
            "tunnel",
            "--no-autoupdate",
            f"--metrics={METRICS_ADDRESS}",
            f"--loglevel={self.config.cloudflared_loglevel}",
            *self.config.run_parameters,
            "run",
        ]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # The token is not in argv: ps output and crash dumps never contain it.
        env[TOKEN_ENV] = self.config.tunnel_token
        return env

    def start(self) -> None:
        """Start the connector (also the way to apply changed options)."""

        self.stop()
        self._bad_token = False
        self._conn_count = 0
        self._ever_connected = False
        argv = self._argv()
        _LOGGER.info("Starting cloudflared (token passed via %s)", TOKEN_ENV)
        try:
            process = self._spawn(argv, self._env())
        except OSError as err:
            self.status.set(STATE_ERROR, f"Could not start cloudflared: {err}")
            self._schedule_restart(BAD_TOKEN_RETRY_SECONDS)
            return
        with self._lock:
            self._process = process
        if process.stdout is not None:
            _Reader(process.stdout, self._on_line).start()
        self.status.started_at = datetime.now(UTC)
        self.status.set(STATE_STARTING)

    def stop(self) -> None:
        """Terminate the connector (SIGTERM, then SIGKILL) and drain its output."""

        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            _LOGGER.info("Stopping cloudflared...")
            try:
                process.terminate()  # SIGTERM: cloudflared unregisters its connections
                process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _LOGGER.warning("cloudflared did not stop in %ds; killing it", STOP_TIMEOUT_SECONDS)
                process.kill()
                process.wait()
        if process.stdout is not None:
            with contextlib.suppress(OSError, ValueError):
                process.stdout.close()
        self._conn_count = 0
        self.status.connections = 0
        self.status.set(STATE_STOPPED)

    def restart(self) -> None:
        """Panel button: a full stop and start, right now (no backoff wait)."""

        self.status.restarts += 1
        self._restart_at = 0.0
        self.start()

    def _schedule_restart(self, delay: float) -> None:
        self._restart_at = self._clock() + delay

    # -- Monitoring ------------------------------------------------------------
    def _on_line(self, line: str) -> None:
        """One cloudflared log line (reader thread): log it and track state."""

        if line.startswith(("ERR ", "FTL ")):
            _LOGGER.error("cloudflared: %s", line)
        elif line.startswith("WRN "):
            _LOGGER.warning("cloudflared: %s", line)
        elif line.startswith(("DBG ", "TRC ")):
            _LOGGER.debug("cloudflared: %s", line)
        else:
            _LOGGER.info("cloudflared: %s", line)
        if version := _VERSION_RE.search(line):
            self.status.version = version.group(1)
        if _REGISTERED_RE.search(line):
            self._conn_count += 1
            self._ever_connected = True
        if _UNREGISTERED_RE.search(line):
            self._conn_count = max(0, self._conn_count - 1)
        if _BAD_TOKEN_RE.search(line):
            self._bad_token = True

    def _metrics_connections(self) -> int | None:
        """The connector's edge-connection count from its localhost metrics page."""

        return _ha_connections(self._metrics())

    def _alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _handle_exit(self) -> None:
        """The connector died: note why, and back off before restarting it."""

        with self._lock:
            process, self._process = self._process, None
        if self._bad_token:
            self.status.set(STATE_ERROR, "Cloudflare rejected the tunnel token. Check it in the Configuration tab.")
            self._schedule_restart(BAD_TOKEN_RETRY_SECONDS)
            return
        code = "unknown" if process is None else str(process.returncode)
        self.status.set(STATE_STOPPED, f"cloudflared exited (code {code}); restarting")
        delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
        self._backoff_index += 1
        self._schedule_restart(delay)
        _LOGGER.warning("cloudflared exited (code %s); restarting in %ds", code, delay)

    def poll(self) -> None:
        """One monitoring pass: check the process, read the connection count, restart if due."""

        if not self._alive():
            if self._process is not None:
                self._handle_exit()
            if self._process is None and self._clock() >= self._restart_at:
                self.status.restarts += 1
                self.start()
            return
        if self._bad_token:
            # Keep the process running (it retries registration itself) but say so.
            self.status.set(STATE_ERROR, "Cloudflare rejected the tunnel token. Check it in the Configuration tab.")
            return
        measured = self._metrics_connections()
        if measured is not None:
            self._conn_count = measured
            if measured > 0:
                self._ever_connected = True
        self.status.connections = self._conn_count
        if self._conn_count > 0:
            if self.status.state != STATE_CONNECTED:
                self._backoff_index = 0  # healthy again: restarts may be quick if it crashes later
            self.status.set(STATE_CONNECTED)
        elif self._ever_connected:
            self.status.set(STATE_DEGRADED, "cloudflared is running but has no edge connection")
        else:
            self.status.set(STATE_STARTING)
