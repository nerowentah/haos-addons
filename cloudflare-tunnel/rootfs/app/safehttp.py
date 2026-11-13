"""Outbound HTTPS with safe defaults (stdlib only, shared by the apps in this repo).

Every call is https:// with certificate and hostname verification (TLS 1.2 or
newer), has a timeout, never follows a redirect to plain http:// and never
reads more than ``max_bytes`` of the answer.

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.error
import urllib.request
from email.message import Message
from typing import IO

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HTTPS = "https://"


class TooLargeError(OSError):
    """The answer is larger than the caller allows."""


def tls_context() -> ssl.SSLContext:
    """Verify the certificate and the hostname; TLS 1.2 or newer."""

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def require_https(url: str) -> str:
    """``url`` when it is an https:// address; ValueError otherwise."""

    if not url.lower().startswith(HTTPS):
        raise ValueError("Only https:// addresses are allowed")
    return url


def read_limited(response: http.client.HTTPResponse | IO[bytes], max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """The whole body, or TooLargeError when it is longer than ``max_bytes``."""

    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise TooLargeError(f"The answer is larger than {max_bytes // 1024} KB")
    return body


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays on https://."""

    def redirect_request(  # noqa: PLR0913, PLR0917 - the signature urllib calls
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.lower().startswith(HTTPS):
            raise urllib.error.HTTPError(newurl, code, "Redirect to a non-https address refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()), _HttpsRedirects())


def fetch(  # noqa: PLR0913 - the parts of a request
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, bytes]:
    """(status, body) of an https:// request.

    Raises ``urllib.error.HTTPError`` for a 4xx/5xx answer (read it with
    :func:`read_limited`), ``urllib.error.URLError``/``OSError`` when the
    server cannot be reached and ``ValueError`` for a non-https address.
    """

    request = urllib.request.Request(require_https(url), data=body, method=method, headers=headers or {})  # noqa: S310 - https only, checked above
    with _opener().open(request, timeout=timeout) as response:
        return int(response.status), read_limited(response, max_bytes)


def get(url: str, headers: dict[str, str] | None = None, *, timeout: float = 30.0) -> bytes:
    """The body of an https:// GET (see :func:`fetch` for what it raises)."""

    return fetch(url, headers=headers, timeout=timeout)[1]
