"""The HTTPS proxy in front of Home Assistant (nginx, run and watched by the app).

nginx listens on port 8443 and serves every certificate the app holds,
picked by the name the client asks for (SNI); the default certificate answers
everything else. Requests (WebSockets included) go to Home Assistant on the
internal network with ``X-Forwarded-For``/``-Proto``, so Home Assistant sees the
real client address once it trusts this app as a proxy.

Home Assistant itself is untouched: http://homeassistant.local:8123, the
Companion apps and ingress keep working.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

NGINX = "/usr/sbin/nginx"
PORT = 8443
CONF_DIR = Path("/data/nginx")
BACKOFF_SECONDS = (2, 5, 10, 30, 60)
APPS_NETWORK = "172.30.32.0/23"  # the Supervisor's networks: Cloudflare Tunnel and other apps connect from here
CLOUDFLARE_IPS = Path(__file__).resolve().parent / "roots" / "cloudflare_ips.txt"

REAL_IP_OFF = "off"
REAL_IP_APPS = "apps"
REAL_IP_CLOUDFLARE = "cloudflare"
REAL_IP_MODES = (REAL_IP_OFF, REAL_IP_APPS, REAL_IP_CLOUDFLARE)

SpawnFn = Callable[[list[str]], "subprocess.Popen[bytes]"]
RunFn = Callable[[list[str]], tuple[int, str]]


@dataclass(slots=True)
class Site:
    """One certificate served by the proxy."""

    names: list[str]
    fullchain: str
    privkey: str


def _server_names(names: list[str], taken: set[str]) -> list[str]:
    """Names for ``server_name``: each name once across all servers (nginx warns on duplicates)."""

    picked = []
    for name in names:
        if name not in taken:
            taken.add(name)
            picked.append(name)
    return picked


def cloudflare_ranges(path: Path = CLOUDFLARE_IPS) -> list[str]:
    """Cloudflare's published IP ranges (vendored; one network per line, # comments)."""

    lines = path.read_text(encoding="ascii").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _real_ip(mode: str, ranges: list[str]) -> str:
    """``http {}`` directives that make nginx (and the X-Forwarded-For it sends) use the visitor's address."""

    if mode == REAL_IP_OFF:
        return ""
    trusted = [APPS_NETWORK] + (list(ranges) if mode == REAL_IP_CLOUDFLARE else [])
    header = "CF-Connecting-IP" if mode == REAL_IP_CLOUDFLARE else "X-Forwarded-For"
    return (
        "".join(f"    set_real_ip_from {network};\n" for network in trusted)
        + f"    real_ip_header {header};\n"
        + "    real_ip_recursive on;\n"
    )


def render(  # noqa: PLR0913 - the options of the proxy
    default: Site,
    others: list[Site],
    upstream: str,
    port: int = PORT,
    *,
    hsts: str = "",
    real_ip: str = REAL_IP_APPS,
    ranges: list[str] | None = None,
) -> str:
    """nginx.conf for the proxy (``hsts``: the Strict-Transport-Security value, empty for none)."""

    location = (
        "        location / {\n"
        f"            proxy_pass {upstream};\n"
        "            proxy_http_version 1.1;\n"
        "            proxy_set_header Host $http_host;\n"
        "            proxy_set_header X-Real-IP $remote_addr;\n"
        "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "            proxy_set_header X-Forwarded-Proto $scheme;\n"
        "            proxy_set_header X-Forwarded-Host $http_host;\n"
        "            proxy_set_header Upgrade $http_upgrade;\n"
        "            proxy_set_header Connection $connection_upgrade;\n"
        "            proxy_buffering off;\n"
        "            proxy_request_buffering off;\n"
        "            proxy_read_timeout 1h;\n"
        "            proxy_send_timeout 1h;\n"
    )
    if upstream.startswith("https://"):
        location += "            proxy_ssl_server_name on;\n            proxy_ssl_verify off;  # Home Assistant on the internal network\n"
    location += "        }\n"

    security = f'        add_header Strict-Transport-Security "{hsts}" always;\n' if hsts else ""

    def server(site: Site, names: list[str], *, default_server: bool) -> str:
        flag = " default_server" if default_server else ""
        server_name = " ".join(names) if names else "_"
        return (
            "    server {\n"
            f"        listen {port} ssl{flag};\n"
            f"        listen [::]:{port} ssl{flag};\n"
            "        http2 on;\n"
            f"        server_name {server_name};\n"
            f"        ssl_certificate {site.fullchain};\n"
            f"        ssl_certificate_key {site.privkey};\n"
            f"{security}"
            f"{location}"
            "    }\n"
        )

    taken: set[str] = set()
    blocks = [server(default, _server_names(default.names, taken), default_server=True)]
    for site in others:
        names = _server_names(site.names, taken)
        if names:  # every name is already served by another certificate
            blocks.append(server(site, names, default_server=False))
    return (
        "# Written by the SSL Cert Manager app; changes are overwritten.\n"
        "worker_processes 1;\n"
        "daemon off;\n"
        f"pid {CONF_DIR}/nginx.pid;\n"
        "error_log stderr warn;\n"
        "events {\n    worker_connections 512;\n}\n"
        "http {\n"
        "    access_log off;\n"
        "    server_tokens off;\n"
        "    client_max_body_size 0;\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n"
        "    ssl_prefer_server_ciphers off;\n"
        "    ssl_session_cache shared:SSL:2m;\n"
        "    ssl_session_timeout 1d;\n"
        "    ssl_session_tickets off;\n"
        "    map $http_upgrade $connection_upgrade {\n        default upgrade;\n        '' close;\n    }\n"
        + _real_ip(real_ip, ranges if ranges is not None else [])
        + "".join(blocks)
        + "}\n"
    )


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603 - fixed argv


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False)  # noqa: S603 - fixed argv
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


class Proxy:
    """Starts, reloads and watches nginx; call :meth:`poll` from the app loop."""

    def __init__(
        self,
        conf_dir: Path = CONF_DIR,
        spawn: SpawnFn = _spawn,
        run: RunFn = _run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conf = conf_dir / "nginx.conf"
        self.spawn = spawn
        self.run = run
        self.clock = clock
        self.process: subprocess.Popen[bytes] | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self._text: str | None = None
        self._backoff = 0
        self._restart_at = 0.0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _test(self, path: Path) -> str | None:
        code, output = self.run([NGINX, "-t", "-q", "-c", str(path)])
        if code == 0:
            return None
        lines = [line for line in output.splitlines() if line.strip()]
        return (lines[-1] if lines else "nginx rejected the configuration")[:300]

    def configure(self, text: str) -> bool:
        """Use ``text`` as nginx.conf: test it first, then start or reload; False (and ``error``) when rejected."""

        if text == self._text and self.running:
            return True
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        candidate = self.conf.with_suffix(".new")
        candidate.write_text(text, encoding="utf-8")
        problem = self._test(candidate)
        if problem:
            self.error = f"nginx: {problem}"
            _LOGGER.error("Proxy configuration rejected: %s", problem)
            candidate.unlink(missing_ok=True)
            return False
        candidate.replace(self.conf)
        self._text = text
        self.error = None
        if self.process is not None and self.running:
            self.process.send_signal(
                getattr(signal, "SIGHUP", 1)
            )  # reload with the new certificates, no dropped connections
            _LOGGER.info("Proxy reloaded")
        else:
            self._start()
        return True

    def _start(self) -> None:
        try:
            self.process = self.spawn([NGINX, "-c", str(self.conf)])
        except OSError as err:
            self.error = f"Could not start nginx: {err}"
            self.process = None
            return
        self.started_at = self.clock()
        if self.process.stdout is not None:
            threading.Thread(target=self._read, args=(self.process,), daemon=True, name="nginx-log").start()
        _LOGGER.info("HTTPS proxy listening on port %d", PORT)

    def _read(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        try:
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    _LOGGER.warning("nginx: %s", line)
                    if "[emerg]" in line:
                        self.error = line[:300]
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        process, self.process = self.process, None
        self._text = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _LOGGER.info("HTTPS proxy stopped")

    def poll(self) -> None:
        """Restart nginx (with back-off) if it died while it should run."""

        if self._text is None or self.running:
            if self.running and self.started_at is not None and self.clock() - self.started_at > 60:  # noqa: PLR2004 - seconds
                self._backoff = 0
            return
        if self.process is not None:
            code = self.process.returncode
            self.process = None
            delay = BACKOFF_SECONDS[min(self._backoff, len(BACKOFF_SECONDS) - 1)]
            self._backoff += 1
            self._restart_at = self.clock() + delay
            self.error = self.error or f"nginx exited (code {code}); restarting"
            _LOGGER.warning("nginx exited (code %s); restarting in %ds", code, delay)
            return
        if self.clock() >= self._restart_at:
            self._start()


def upstream_for(core_info: dict[str, object]) -> str:
    """Home Assistant on the internal network, as the proxy reaches it."""

    port = int(str(core_info.get("port") or 8123))
    scheme = "https" if core_info.get("ssl") else "http"
    return f"{scheme}://homeassistant:{port}"
