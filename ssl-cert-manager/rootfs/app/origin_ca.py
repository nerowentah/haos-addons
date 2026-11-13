"""Cloudflare Origin CA certificates (``POST /certificates``).

An Origin certificate is trusted by Cloudflare only, not by browsers: it is
what the server behind Cloudflare (here: Home Assistant, reached through the
tunnel) shows to Cloudflare. The key is made here; only the CSR is sent.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from collections.abc import Callable
from pathlib import Path

import safehttp

_LOGGER = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
VALIDITY_DAYS = 5475  # 15 years, the longest Cloudflare offers
ROOTS_DIR = Path(__file__).resolve().parent / "roots"
ROOT_FILES = ("origin_ca_ecc_root.pem", "origin_ca_rsa_root.pem")

HttpFn = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class OriginCaError(RuntimeError):
    """The Cloudflare API refused (the message is safe to show)."""


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    try:
        return safehttp.fetch(url, method=method, headers=headers, body=body, timeout=30)
    except urllib.error.HTTPError as err:
        return int(err.code), safehttp.read_limited(err)
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise OriginCaError(f"Cloudflare API not reachable: {err}") from err


def roots_pem() -> str:
    """Cloudflare's Origin CA roots (ECC and RSA), as published by Cloudflare."""

    return "".join((ROOTS_DIR / name).read_text(encoding="ascii") for name in ROOT_FILES)


def _errors(payload: object) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = [f"{e.get('message')} ({e.get('code')})" for e in errors if isinstance(e, dict)]
            return "; ".join(parts)
    return "no details"


class OriginCa:
    def __init__(self, http: HttpFn = _http) -> None:
        self.http = http

    def _call(self, method: str, path: str, token: str, body: dict[str, object] | None = None) -> dict[str, object]:
        if not token:
            raise OriginCaError("Set cloudflare_api_token in the Configuration tab first")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ssl-cert-manager-app",
        }
        status, raw = self.http(method, f"{API}{path}", headers, None if body is None else json.dumps(body).encode())
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if status == 403 or status == 401:  # noqa: PLR1714, PLR2004 - HTTP statuses
            raise OriginCaError(
                "Cloudflare refused the API token: it needs Zone → SSL and Certificates → Edit for this zone "
                f"({_errors(payload)})"
            )
        if status >= 400 or not isinstance(payload, dict) or not payload.get("success"):  # noqa: PLR2004 - HTTP status
            raise OriginCaError(f"Cloudflare Origin CA: {_errors(payload)} (HTTP {status})")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def create(self, token: str, hostnames: list[str], csr: str) -> tuple[str, str]:
        """``(certificate_pem, cloudflare_certificate_id)`` for ``csr``."""

        result = self._call(
            "POST",
            "/certificates",
            token,
            {"hostnames": hostnames, "requested_validity": VALIDITY_DAYS, "request_type": "origin-ecc", "csr": csr},
        )
        pem = str(result.get("certificate") or "")
        if "BEGIN CERTIFICATE" not in pem:
            raise OriginCaError("Cloudflare returned no certificate")
        _LOGGER.info("Cloudflare Origin certificate issued for %s", ", ".join(hostnames))
        return pem, str(result.get("id") or "")

    def revoke(self, token: str, certificate_id: str) -> None:
        self._call("DELETE", f"/certificates/{certificate_id}", token)
        _LOGGER.info("Cloudflare Origin certificate revoked")
