"""Where the tunnel sends Home Assistant's traffic, and how it checks the certificate there.

The SSL Cert Manager app (same repository) publishes its certificates and a
``status.json`` under ``/ssl/ssl-cert-manager/``. With it, the tunnel no
longer needs ``noTLSVerify``:

* its HTTPS proxy is on: traffic goes to ``https://<that app>:8443``, checked
  against its trust bundle (Cloudflare's Origin CA roots + imported CAs; the
  system CAs still count, so Let's Encrypt works too) for a name the proxy
  has a certificate for;
* Home Assistant itself serves its certificate: same check, straight to
  ``https://homeassistant:8123``;
* otherwise: plain ``http://homeassistant:8123`` on the internal network, or
  (Home Assistant on HTTPS with a certificate the app does not know) the old
  ``noTLSVerify`` in ``auto`` mode - ``verify`` refuses that, ``skip`` always uses it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MANAGER_STATUS = Path("/ssl/ssl-cert-manager/status.json")
TUNNEL_STATUS = Path("/ssl/cloudflare-tunnel/status.json")
DEFAULT_CORE_PORT = 8123

ORIGIN_AUTO = "auto"
ORIGIN_VERIFY = "verify"
ORIGIN_SKIP = "skip"
ORIGIN_MODES = (ORIGIN_AUTO, ORIGIN_VERIFY, ORIGIN_SKIP)


@dataclass(frozen=True, slots=True)
class Origin:
    """The service the ingress rule points at, and its ``originRequest`` TLS settings."""

    service: str
    no_tls_verify: bool = False
    ca_pool: str | None = None
    server_name: str | None = None
    note: str = ""

    @property
    def tls(self) -> bool:
        return self.service.startswith("https://")

    def request(self) -> dict[str, object]:
        """``originRequest`` entries for config.yml (empty for plain http)."""

        settings: dict[str, object] = {}
        if self.no_tls_verify:
            settings["noTLSVerify"] = True
        if self.ca_pool:
            settings["caPool"] = self.ca_pool
        if self.server_name:
            settings["originServerName"] = self.server_name
        return settings

    def describe(self) -> str:
        if not self.tls:
            return f"{self.service} (internal network, no TLS)"
        if self.no_tls_verify:
            return f"{self.service} (certificate not checked)"
        name = f" for {self.server_name}" if self.server_name else ""
        return f"{self.service} (certificate checked{name})"


def read_manager(path: Path = MANAGER_STATUS) -> dict[str, object] | None:
    """SSL Cert Manager's status.json, or None when that app is not there."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == 1 else None


def covers(names: list[str], hostname: str) -> bool:
    host = hostname.lower()
    for name in (n.lower() for n in names):
        if name == host or (name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]):
            return True
    return False


def pick_name(names: list[str], hostname: str) -> str | None:
    """A name the certificate(s) cover: the public hostname if possible, else the first plain name."""

    if hostname and covers(names, hostname):
        return hostname
    plain = [n for n in names if not n.startswith("*.")]
    if plain:
        return plain[0]
    return f"origin.{names[0][2:]}" if names else None  # a wildcard covers any single label


def _names(entries: object) -> list[str]:
    found: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            found += [str(n) for n in entry.get("names") or [] if str(n) not in found]
    return found


def _bundle(manager: dict[str, object]) -> str | None:
    path = manager.get("trust_bundle")
    return str(path) if path and Path(str(path)).is_file() else None


def plan(mode: str, core: dict[str, object], manager: dict[str, object] | None, hostname: str) -> Origin:
    """The origin for the ingress rule of ``hostname``."""

    try:
        port = int(str(core.get("port") or DEFAULT_CORE_PORT))
    except ValueError:
        port = DEFAULT_CORE_PORT
    ssl = bool(core.get("ssl"))
    home_assistant = f"{'https' if ssl else 'http'}://homeassistant:{port}"
    if mode == ORIGIN_SKIP:
        note = "origin_tls is skip: Home Assistant's certificate is not checked" if ssl else ""
        return Origin(home_assistant, no_tls_verify=ssl, note=note)

    bundle = _bundle(manager) if manager else None
    proxy = manager.get("proxy") if manager else None
    if isinstance(proxy, dict) and proxy.get("enabled") and proxy.get("host") and bundle:
        names = _names(manager.get("certificates") if manager else None)
        return Origin(
            f"https://{proxy['host']}:{int(str(proxy.get('port') or 8443))}",
            ca_pool=bundle,
            server_name=pick_name(names, hostname),
            note="through SSL Cert Manager's HTTPS proxy",
        )
    if not ssl:
        return Origin(home_assistant)
    default = manager.get("default") if manager else None
    if manager and manager.get("ha_https") and isinstance(default, dict) and bundle:
        return Origin(
            home_assistant,
            ca_pool=bundle,
            server_name=pick_name([str(n) for n in default.get("names") or []], hostname),
            note="Home Assistant's certificate from SSL Cert Manager",
        )
    if mode == ORIGIN_VERIFY:
        return Origin(home_assistant, ca_pool=bundle, server_name=hostname or None, note="origin_tls is verify")
    return Origin(
        home_assistant,
        no_tls_verify=True,
        note="Home Assistant's certificate is not checked; manage it with SSL Cert Manager to check it",
    )
