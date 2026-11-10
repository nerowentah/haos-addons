"""Ranhill SAJ express-payment client: look up the amount due of a water account (stdlib only).

The public Quick Payment flow needs no login:
1. GET /expresspayment/checkout/search renders the form and a Magento ``form_key``.
2. POST the account number, one character per field (digit-1..8, digit-10, digit-11..17),
   to /expresspayment/checkout/verification.
3. A known account renders the "Verify Your Account" page with the holder's name and
   "Amount Due"; an unknown one redirects back to /search/.

The site is unofficial for this use and may change without notice. Account numbers,
names, addresses and cookies are never logged.
"""

from __future__ import annotations

import gzip
import html
import http.cookiejar
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.ranhillsaj.com.my"
HOST = "www.ranhillsaj.com.my"
SEARCH_PATH = "/expresspayment/checkout/search"
VERIFICATION_PATH = "/expresspayment/checkout/verification"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
# Akamai in front of the site rejects requests that do not look like a browser.
HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ms;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}
HTTP_TIMEOUT = 30
HTTP_OK = 200
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_PAGE_BYTES = 2 * 1024 * 1024

FORM_KEY_RE = re.compile(r'name="form_key"[^>]*\bvalue="([^"]+)"|value="([^"]+)"[^>]*name="form_key"')
VERIFY_MARKER_RE = re.compile(r'id="tab2"|action="/expresspayment/checkout/summary"')
SEARCH_MARKER_RE = re.compile(r'id="tab1"|Search Bill by Account Number')
# <div class="payment-amount"> <p class="label">Amount Due</p> <p class="amount">RM 12.34</p>
AMOUNT_DUE_RE = re.compile(
    r'<p[^>]*class="[^"]*\blabel\b[^"]*"[^>]*>\s*Amount\s+Due\s*</p>\s*<p[^>]*class="[^"]*\bamount\b[^"]*"[^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
HOLDER_RE = re.compile(
    r'<div[^>]*class="[^"]*\bpayment-address\b[^"]*"[^>]*>\s*<strong[^>]*>(.*?)</strong>', re.IGNORECASE | re.DOTALL
)
TAG_RE = re.compile(r"<[^>]+>")
AMOUNT_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
CREDIT_RE = re.compile(r"(^|\s)-|\(\s*[\d.,]+\s*\)|\bCR\b|\bkredit\b|\bcredit\b", re.IGNORECASE)


class SajError(Exception):
    """Base error for the Ranhill SAJ client."""


class AccountNotFoundError(SajError):
    """The site does not know the account number (it redirects back to the search page)."""


class SiteError(SajError):
    """Transient network or server-side failure."""


class ParseError(SajError):
    """The page loaded but its layout is not the one this app understands (the site changed)."""

    def __init__(self, message: str, page: str = "") -> None:
        super().__init__(message)
        self.page = page


@dataclass(frozen=True, slots=True)
class Bill:
    """What the verification page shows for an account."""

    amount_due: float
    amount_text: str
    holder: str = ""


def form_fields(account_number: str, form_key: str) -> list[tuple[str, str]]:
    """The POST body of the search form: one character per field (the letter goes to ``digit-10``)."""

    number = account_number.upper()
    if not re.fullmatch(r"\d{8}[A-Z]\d{7}", number):
        raise ValueError("account number must be 8 digits, a letter and 7 digits")
    names = [f"digit-{i}" for i in range(1, 9)] + ["digit-10"] + [f"digit-{i}" for i in range(11, 18)]
    return [("form_key", form_key), *zip(names, number, strict=True)]


def extract_form_key(page: str) -> str:
    """The Magento form key rendered into the search page."""

    match = FORM_KEY_RE.search(page)
    if not match:
        raise ParseError("The search page has no form key; the site layout may have changed", page)
    return match.group(1) or match.group(2)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub(" ", fragment)).split())


def parse_amount(text: str) -> float:
    """``"RM 1,234.56"`` -> 1234.56; a credit (``-12.30``, ``(12.30)``, ``12.30 CR``) is negative."""

    cleaned = re.sub(r"\bRM\b", " ", text, flags=re.IGNORECASE).strip()
    match = AMOUNT_RE.search(cleaned)
    if not match:
        raise ValueError(f"no amount in {text!r}")
    whole = match.group(1).replace(",", "")
    cents = (match.group(2) or "0").ljust(2, "0")
    value = round(int(whole) + int(cents) / 100, 2)
    return -value if value and CREDIT_RE.search(cleaned) else value


def parse_verification(page: str) -> Bill:
    """Read the amount due (and holder) from the verification page.

    The site renders the same template, with empty values, when it redirects an unknown
    account back to the search page, so an empty amount means "not found".
    """

    if not VERIFY_MARKER_RE.search(page):
        if SEARCH_MARKER_RE.search(page):
            raise AccountNotFoundError("Ranhill SAJ does not recognise this account number")
        raise ParseError("The verification page was not returned; the site layout may have changed", page)
    match = AMOUNT_DUE_RE.search(page)
    if not match:
        raise ParseError("The verification page has no 'Amount Due'; the site layout may have changed", page)
    amount_text = _text(match.group(1))
    if not amount_text:
        raise AccountNotFoundError("Ranhill SAJ does not recognise this account number")
    try:
        amount = parse_amount(amount_text)
    except ValueError as err:
        raise ParseError("'Amount Due' is not a number; the site layout may have changed", page) from err
    holder = HOLDER_RE.search(page)
    return Bill(amount_due=amount, amount_text=amount_text, holder=_text(holder.group(1)) if holder else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand redirects back to the caller: a redirect to /search/ is how the site says "unknown account"."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    location: str
    body: str


class SajClient:
    """One lookup session (fresh cookies each time, so stale sessions never linger)."""

    def __init__(self, timeout: float = HTTP_TIMEOUT) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def _set_cookie(self, name: str, value: str) -> None:
        self.jar.set_cookie(
            http.cookiejar.Cookie(
                0, name, value, None, False, HOST, False, False, "/", True, True, None, False, None, None, {}
            )
        )

    def _request(self, path: str, data: list[tuple[str, str]] | None = None, referer: str = "") -> _Reply:
        headers = dict(HTML_HEADERS)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = BASE_URL
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(BASE_URL + path, data=body, headers=headers)  # noqa: S310 - fixed https host
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return _Reply(response.status, "", self._decode(response.read(MAX_PAGE_BYTES), response.headers))
        except urllib.error.HTTPError as err:
            if err.code in REDIRECT_STATUSES:
                return _Reply(
                    err.code, err.headers.get("Location") or "", self._decode(err.read(MAX_PAGE_BYTES), err.headers)
                )
            raise SiteError(f"HTTP {err.code} from Ranhill SAJ") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise SiteError(f"Could not reach Ranhill SAJ: {reason}") from err

    @staticmethod
    def _decode(raw: bytes, headers: object) -> str:
        encoding = str(getattr(headers, "get", lambda *_: "")("Content-Encoding") or "").lower()
        try:
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                raw = zlib.decompress(raw)
        except (OSError, zlib.error) as err:
            raise SiteError("Ranhill SAJ sent a corrupt reply") from err
        return raw.decode("utf-8", errors="replace")

    def lookup(self, account_number: str) -> Bill:
        """Fetch the amount due for one account."""

        search_url = BASE_URL + SEARCH_PATH
        search = self._request(SEARCH_PATH)
        if search.status != HTTP_OK:
            raise SiteError(f"The search page answered HTTP {search.status}")
        form_key = extract_form_key(search.body)
        self._set_cookie("form_key", form_key)  # Magento's JS copies the key into a cookie; the server checks both
        reply = self._request(VERIFICATION_PATH, form_fields(account_number, form_key), referer=search_url)
        if reply.status in REDIRECT_STATUSES:
            target = urllib.parse.urlsplit(urllib.parse.urljoin(BASE_URL, reply.location)).path.rstrip("/")
            if target == SEARCH_PATH:
                raise AccountNotFoundError("Ranhill SAJ does not recognise this account number")
            if target != VERIFICATION_PATH:
                raise ParseError("Ranhill SAJ redirected somewhere unexpected; the site may have changed", reply.body)
            reply = self._request(VERIFICATION_PATH, referer=search_url)
        if reply.status != HTTP_OK:
            raise SiteError(f"The verification page answered HTTP {reply.status}")
        return parse_verification(reply.body)
