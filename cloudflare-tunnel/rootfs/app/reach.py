"""Request ``https://<hostname>/api/`` the way the Internet does, and say why when it fails.

Shared verbatim by Cloudflare Tunnel and SSL Cert Manager (stdlib only).

The check runs inside the app container, so it uses Home Assistant's DNS. A
local DNS override (the hostname pointing at the LAN address of Home
Assistant, common for local HTTPS) would send it to ``<LAN IP>:443`` instead of
Cloudflare. When every address the local DNS gives is private, the public
addresses are looked up through Cloudflare's DNS-over-HTTPS and the request
goes there, still with the hostname for SNI and certificate checking.

Every failure becomes a short reason for the panel: the name does not
resolve, the TLS certificate does not match, the connection was refused, or
nothing answered in time.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

import safehttp

TIMEOUT_SECONDS = 15
DOH_URL = "https://1.1.1.1/dns-query"
DOH_TIMEOUT_SECONDS = 10
USER_AGENT = "home-assistant-app-reach-check"
MAX_TARGETS = 2  # addresses tried before giving up (Cloudflare answers on all of them alike)

ResolveFn = Callable[[str], list[str]]
FetchFn = Callable[[str, str, str], int]  # (ip, hostname, path) -> HTTP status; raises on failure


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of one request: an HTTP status, or why there was none."""

    code: int | None
    reason: str = ""
    addresses: tuple[str, ...] = ()
    local: tuple[str, ...] = ()  # private addresses from the local DNS that were bypassed

    @property
    def ok(self) -> bool:
        """Home Assistant answered (401: it wants a login, which proves it is there)."""

        return self.code in (200, 401)

    @property
    def note(self) -> str:
        if not self.local:
            return ""
        return f"local DNS points to {self.local[0]}; checked through public DNS"


def _routable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global


def system_resolve(hostname: str) -> list[str]:
    """Addresses from the container's DNS, IPv4 first, without duplicates."""

    infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    found = [str(info[4][0]) for info in sorted(infos, key=lambda i: i[0] != socket.AF_INET)]
    return list(dict.fromkeys(found))


def doh_resolve(hostname: str) -> list[str]:
    """IPv4 addresses from Cloudflare's public DNS-over-HTTPS (bypasses local overrides)."""

    query = urllib.parse.urlencode({"name": hostname, "type": "A"})
    reply = safehttp.get(
        f"{DOH_URL}?{query}",
        {"accept": "application/dns-json", "User-Agent": USER_AGENT},
        timeout=DOH_TIMEOUT_SECONDS,
    )
    data = json.loads(reply.decode("utf-8"))
    answers = data.get("Answer") if isinstance(data, dict) else None
    return [str(a["data"]) for a in answers or [] if isinstance(a, dict) and a.get("type") == 1 and a.get("data")]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a chosen address, with the hostname for SNI and certificate checking."""

    def __init__(self, hostname: str, address: str, context: ssl.SSLContext) -> None:
        super().__init__(hostname, 443, timeout=TIMEOUT_SECONDS, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)


def fetch(address: str, hostname: str, path: str) -> int:
    """GET ``https://<hostname><path>`` from ``address``; the HTTP status (any status counts as an answer)."""

    connection = _PinnedHTTPSConnection(hostname, address, ssl.create_default_context())
    try:
        connection.request("GET", path, headers={"Host": hostname, "User-Agent": USER_AGENT})
        return int(connection.getresponse().status)
    finally:
        connection.close()


def _short(err: BaseException) -> str:
    text = str(err.args[-1] if isinstance(err, OSError) and err.args else err) or type(err).__name__
    return text[:160]


def _tls_reason(err: ssl.SSLCertVerificationError, hostname: str) -> str:
    message = err.verify_message or _short(err)
    reason = f"TLS: {message}"
    if "mismatch" in message.lower() and hostname.count(".") >= 3:  # noqa: PLR2004 - a.b.example.com
        reason += " (on Cloudflare, the free certificate covers one level only: *.example.com, not a.b.example.com)"
    return reason


def _handshake_reason(hostname: str, address: str) -> str:
    """Cloudflare's edge refuses the handshake (alert 40) when no edge certificate covers the SNI name."""

    reason = f"TLS: {address} refused the handshake for {hostname}, so Cloudflare has no edge certificate for it"
    if hostname.count(".") >= 3:  # noqa: PLR2004 - a.b.example.com
        return reason + (
            " (the free certificate covers example.com and *.example.com only: use a one-level name"
            " such as home.example.com, or add an Advanced Certificate)"
        )
    return reason + (
        " (in the Cloudflare dashboard, SSL/TLS > Edge Certificates: Universal SSL must be enabled and Active,"
        " which can take up to 24 hours on a new domain; the DNS record must be Proxied)"
    )


def probe(
    hostname: str,
    path: str = "/api/",
    *,
    resolve: ResolveFn = system_resolve,
    doh: ResolveFn = doh_resolve,
    fetch: FetchFn = fetch,
) -> Result:
    """One request to the public hostname, bypassing a local DNS override."""

    lookup_error: str | None = None
    try:
        addresses = resolve(hostname)
    except (OSError, UnicodeError) as err:
        addresses, lookup_error = [], _short(err)
    public = [a for a in addresses if _routable(a)]
    local: tuple[str, ...] = ()
    if not public:
        try:
            public = [a for a in doh(hostname) if _routable(a)]
        except (OSError, ValueError):
            public = []
        if public and addresses:
            local = tuple(addresses)
    if not public and not addresses:
        detail = f" ({lookup_error})" if lookup_error else ""
        return Result(None, f"{hostname} does not resolve from Home Assistant or in public DNS{detail}")
    targets = public or addresses  # only private addresses and no public answer: try them, and say so
    override = "" if public else " (local DNS override; the name has no public address)"
    reason = ""
    for address in targets[:MAX_TARGETS]:
        try:
            code = fetch(address, hostname, path)
        except (OSError, http.client.HTTPException) as err:
            reason, final = _failure(err, hostname, address)
            if final:
                return Result(None, reason, tuple(targets), local)
        else:
            return Result(code, "", tuple(targets), local)
    return Result(None, reason + override, tuple(targets), local)


def _failure(err: OSError | http.client.HTTPException, hostname: str, address: str) -> tuple[str, bool]:
    """(reason, final): final when every edge address would fail alike (a certificate problem)."""

    if isinstance(err, ssl.SSLCertVerificationError):
        return _tls_reason(err, hostname), True
    if isinstance(err, ssl.SSLError) and "HANDSHAKE_FAILURE" in str(err.reason or err):
        return _handshake_reason(hostname, address), True
    if isinstance(err, ssl.SSLError):
        return f"TLS: {err.reason or _short(err)}", False
    if isinstance(err, ConnectionRefusedError):
        return f"connection refused by {address}", False
    if isinstance(err, TimeoutError):
        return f"no answer within {TIMEOUT_SECONDS} s from {address}", False
    return f"network error with {address} ({_short(err)})", False


def explain(result: Result, describe: Callable[[int | None], tuple[str, str]]) -> tuple[str, str]:
    """(text, tone) for the panel: the app's wording for HTTP codes, the reason when there was no answer."""

    if result.code is None:
        return f"No answer from the public hostname: {result.reason or 'unknown reason'}", "warn"
    text, tone = describe(result.code)
    return (f"{text} ({result.note})" if result.note else text), tone
