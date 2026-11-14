"""Sign in to Cloudflare and create the tunnel (login mode).

The steps are the ones Cloudflare documents for a locally managed tunnel,
run with ``cloudflared`` itself:

1. ``cloudflared tunnel login`` prints a dash.cloudflare.com link. The user
   opens it, picks a domain and authorizes; cloudflared then saves
   ``$HOME/.cloudflared/cert.pem`` (we set ``HOME`` to ``/data/cloudflared``).
2. ``cloudflared tunnel create`` makes the tunnel and writes its credentials.
3. ``cloudflared tunnel route dns`` points the public hostname at it (CNAME).
4. ``config.yml`` maps that hostname to Home Assistant; ``tunnel run`` uses it.

``cert.pem`` holds an API token for the chosen zone. It stays in /data with
mode 0600 and is never logged or shown in the panel.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shutil
import threading
import urllib.error
from collections.abc import Callable
from pathlib import Path

import cloudflared as cf
import safehttp
from safefile import atomic_write

_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("CF_TUNNEL_DATA_DIR", "/data/cloudflared"))
TUNNEL_NAME = "home-assistant"
NAME_ATTEMPTS = 5  # home-assistant, home-assistant-2 ... -5
LOGIN_TIMEOUT_SECONDS = 11 * 60  # cloudflared long-polls 10 x 60s
COMMAND_TIMEOUT_SECONDS = 120
CF_API = "https://api.cloudflare.com/client/v4"

LOGIN_IDLE = "idle"
LOGIN_STARTING = "starting"
LOGIN_WAITING = "waiting"  # the link is ready: waiting for the user to authorize
LOGIN_DONE = "done"
LOGIN_FAILED = "failed"

ROUTE_OK = "ok"
ROUTE_EXISTS = "exists"

_LOGIN_URL_RE = re.compile(r"https://dash\.(?:fed\.)?cloudflare\.com/argotunnel\?\S+")
_CREATED_RE = re.compile(r"Created tunnel (\S+) with id ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+-----\s*(.*?)\s*-----END [A-Z ]+-----", re.DOTALL)
_LOG_PREFIX_RE = re.compile(r"^\S+Z\s+(?:INF|WRN|ERR|FTL|DBG)\s+")

HttpGet = Callable[[str, dict[str, str]], bytes]


class AccountError(RuntimeError):
    """A cloudflared step failed; the message is shown in the panel."""


def _http_get(url: str, headers: dict[str, str]) -> bytes:
    return safehttp.get(url, headers, timeout=15)


def _yaml_scalar(value: object) -> str:
    """A config.yml value: booleans as YAML booleans, strings quoted when needed."""

    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z0-9_./*-]+", text) else json.dumps(text)


def _last_message(output: str) -> str:
    """The most useful line of cloudflared's output for the panel (log prefixes removed)."""

    lines = [_LOG_PREFIX_RE.sub("", line).strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"error|failed|not|invalid|denied", line, re.IGNORECASE)]
    return (errors or lines or ["cloudflared gave no output"])[-1][:300]


class Login:
    """One ``cloudflared tunnel login`` run, read on its own thread."""

    def __init__(self, account: Account) -> None:
        self._account = account
        self.state = LOGIN_IDLE
        self.url: str | None = None
        self.error: str | None = None
        self._process: object | None = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.state in (LOGIN_STARTING, LOGIN_WAITING)

    def start(self) -> None:
        """Start signing in (no-op while a sign-in is already running)."""

        with self._lock:
            if self.active:
                return
            self.state, self.url, self.error = LOGIN_STARTING, None, None
        self._account.prepare()
        try:
            process = self._account.spawn([cf.CLOUDFLARED, "tunnel", "login"], self._account.env())
        except OSError as err:
            self._finish(LOGIN_FAILED, f"Could not start cloudflared: {err}")
            return
        self._process = process
        threading.Thread(target=self._read, args=(process,), daemon=True, name="cloudflared-login").start()
        timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, self._expire, args=(process,))
        timer.daemon = True
        timer.start()
        _LOGGER.info("Waiting for the Cloudflare sign-in (open it from the Cloudflare Tunnel panel)")

    def _read(self, process: object) -> None:
        output: list[str] = []
        stdout = getattr(process, "stdout", None)
        try:
            for raw in stdout or ():
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                output.append(line)
                found = _LOGIN_URL_RE.search(line)
                if found and self.url is None:
                    self.url = found.group(0)
                    self.state = LOGIN_WAITING
        except (OSError, ValueError):
            pass
        code = process.wait()  # type: ignore[attr-defined]
        if self._account.signed_in():
            self._finish(LOGIN_DONE)
        elif self.state != LOGIN_FAILED:
            reason = _last_message("\n".join(output))
            self._finish(LOGIN_FAILED, f"Sign-in did not finish (cloudflared exit {code}): {reason}")

    def _expire(self, process: object) -> None:
        if self.active and process is self._process:
            self._finish(LOGIN_FAILED, "The sign-in link expired; start again")
            with contextlib.suppress(OSError):
                process.kill()  # type: ignore[attr-defined]

    def _finish(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        if state == LOGIN_DONE:
            _LOGGER.info("Signed in to Cloudflare")
        elif error:
            _LOGGER.warning("Cloudflare sign-in: %s", error)


class Account:
    """The files in /data/cloudflared and the cloudflared commands that manage them."""

    def __init__(
        self,
        base: Path = DATA_DIR,
        spawn: cf.SpawnFn = cf._spawn,
        http_get: HttpGet = _http_get,
    ) -> None:
        self.base = base
        self.cert = base / ".cloudflared" / "cert.pem"
        self.credentials = base / "tunnel.json"
        self.ingress = base / "config.yml"
        self.previous = base / "config.yml.prev"
        self.spawn = spawn
        self.http_get = http_get
        self.login = Login(self)
        self._zone: str | None = None

    # -- files ----------------------------------------------------------------------
    def prepare(self) -> None:
        (self.base / ".cloudflared").mkdir(parents=True, exist_ok=True)
        for directory in (self.base, self.base / ".cloudflared"):
            directory.chmod(0o700)

    def env(self) -> dict[str, str]:
        """cloudflared's environment: our HOME, and never a tunnel token."""

        env = dict(os.environ)
        env.pop(cf.TOKEN_ENV, None)
        env["HOME"] = str(self.base)
        return env

    def signed_in(self) -> bool:
        try:
            return self.cert.stat().st_size > 0
        except OSError:
            return False

    def tunnel_id(self) -> str | None:
        """The tunnel this app created (from its credentials file), or None."""

        try:
            data = json.loads(self.credentials.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("TunnelID") if isinstance(data, dict) else None
        return str(value) if value else None

    def _cert_fields(self) -> dict[str, object]:
        """The JSON inside cert.pem (zoneID, accountID, apiToken); {} when unreadable."""

        try:
            text = self.cert.read_text(encoding="utf-8")
        except OSError:
            return {}
        for block in _PEM_RE.findall(text):
            try:
                data = json.loads(base64.b64decode("".join(block.split())))
            except (binascii.Error, ValueError):
                continue
            if isinstance(data, dict) and data.get("zoneID"):
                return data
        return {}

    @property
    def zone(self) -> str | None:
        """The domain looked up by :meth:`zone_name`, without a lookup (for the panel thread)."""

        return self._zone or None

    def zone_name(self) -> str | None:
        """The domain picked at sign-in (e.g. example.com), looked up once; None when unknown."""

        if self._zone is not None:
            return self._zone or None
        fields = self._cert_fields()
        zone_id, api_token = fields.get("zoneID"), fields.get("apiToken")
        if not zone_id or not api_token:
            return None
        try:
            reply = json.loads(self.http_get(f"{CF_API}/zones/{zone_id}", {"Authorization": f"Bearer {api_token}"}))
        except (urllib.error.URLError, OSError, ValueError):
            _LOGGER.debug("Could not look up the Cloudflare zone name")
            self._zone = ""  # only a pre-fill: do not retry on every pass
            return None
        result = reply.get("result") if isinstance(reply, dict) else None
        name = result.get("name") if isinstance(result, dict) else None
        self._zone = str(name).lower() if name else ""
        return self._zone or None

    # -- cloudflared commands -------------------------------------------------------
    def _run(self, *args: str) -> tuple[int, str]:
        """Run ``cloudflared tunnel --origincert <cert> <args>``: (exit code, output)."""

        argv = [cf.CLOUDFLARED, "tunnel", "--origincert", str(self.cert), *args]
        try:
            process = self.spawn(argv, self.env())
        except OSError as err:
            raise AccountError(f"Could not start cloudflared: {err}") from err
        timer = threading.Timer(COMMAND_TIMEOUT_SECONDS, process.kill)
        timer.daemon = True
        timer.start()
        try:
            output = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
            code = process.wait()
        finally:
            timer.cancel()
        return code, output

    def create_tunnel(self) -> tuple[str, str]:
        """Create the tunnel: (id, name). Tries home-assistant, home-assistant-2... when a name is taken."""

        if not self.signed_in():
            raise AccountError("Sign in to Cloudflare first")
        self.prepare()
        output = ""
        for attempt in range(1, NAME_ATTEMPTS + 1):
            name = TUNNEL_NAME if attempt == 1 else f"{TUNNEL_NAME}-{attempt}"
            self.credentials.unlink(missing_ok=True)  # create refuses to overwrite it
            code, output = self._run("create", "--credentials-file", str(self.credentials), name)
            created = _CREATED_RE.search(output)
            if code == 0 and created:
                if not self.credentials.exists():
                    raise AccountError("cloudflared created the tunnel but wrote no credentials file")
                self.credentials.chmod(0o600)
                _LOGGER.info("Created tunnel %s (%s)", created.group(1), created.group(2))
                return created.group(2), created.group(1)
            if "already exists" not in output.lower():
                break
        raise AccountError(f"Could not create the tunnel: {_last_message(output)}")

    def route_dns(self, tunnel_id: str, hostname: str, *, overwrite: bool = False) -> str:
        """Point ``hostname`` at the tunnel: ROUTE_OK, or ROUTE_EXISTS when another record has the name."""

        args = ["route", "dns", *(["--overwrite-dns"] if overwrite else []), tunnel_id, hostname]
        code, output = self._run(*args)
        if code == 0:
            _LOGGER.info("DNS: %s routes to the tunnel", hostname)
            return ROUTE_OK
        if "already exists" in output.lower():
            return ROUTE_EXISTS
        raise AccountError(f"Could not create the DNS record for {hostname}: {_last_message(output)}")

    def write_ingress(
        self,
        tunnel_id: str,
        hostname: str,
        service: str,
        *,
        origin_request: dict[str, object] | None = None,
    ) -> bool:
        """Write config.yml: ``hostname`` goes to Home Assistant, anything else gets a 404.

        The config it replaces is kept as ``config.yml.prev`` until the change is
        verified (:meth:`discard_previous`) or undone (:meth:`restore_previous`).
        Returns False when the file already said exactly this.
        """

        self.prepare()
        origin = ""
        if origin_request:
            origin = "    originRequest:\n" + "".join(
                f"      {key}: {_yaml_scalar(value)}\n" for key, value in origin_request.items()
            )
        text = (
            "# Written by the Cloudflare Tunnel app; changes are overwritten.\n"
            f"tunnel: {tunnel_id}\n"
            f"credentials-file: {self.credentials}\n"
            "ingress:\n"
            f"  - hostname: {hostname}\n"
            f"    service: {service}\n"
            f"{origin}"
            "  - service: http_status:404\n"
        )
        try:
            current = self.ingress.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == text:
            return False
        if current is not None:
            atomic_write(self.previous, current)
        atomic_write(self.ingress, text)
        _LOGGER.info("Ingress: %s → %s", hostname, service)
        return True

    def has_previous(self) -> bool:
        return self.previous.is_file()

    def restore_previous(self) -> bool:
        """Put the config.yml from before the last change back; False when there is none."""

        try:
            self.previous.replace(self.ingress)
        except OSError:
            return False
        _LOGGER.warning("Ingress: restored the previous configuration")
        return True

    def discard_previous(self) -> None:
        self.previous.unlink(missing_ok=True)

    def sign_out(self) -> str | None:
        """Delete the tunnel at Cloudflare (best effort) and forget the sign-in; returns a warning or None."""

        warning = None
        tunnel_id = self.tunnel_id()
        if tunnel_id and self.signed_in():
            try:
                code, output = self._run("delete", "-f", tunnel_id)
            except AccountError as err:
                code, output = 1, str(err)
            if code != 0:
                warning = (
                    f"The tunnel could not be deleted at Cloudflare ({_last_message(output)}); delete it in Zero Trust"
                )
        elif tunnel_id:
            warning = "Not signed in, so the tunnel was left at Cloudflare; delete it in Zero Trust"
        for path in (self.cert, self.credentials, self.ingress, self.previous):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.base / ".cloudflared", ignore_errors=True)
        self._zone = None
        self.login = Login(self)
        _LOGGER.info("Signed out of Cloudflare")
        return warning
