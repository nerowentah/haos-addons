"""myTNB smart meter portal client (stdlib only).

Port of the browser-replay flow from feiming/homeassistant-MyTNB:
login -> SSO handler -> smart meter page -> smartliving dashboard (sdpudcid)
-> per metric: "arm" the commodity page, then query the timeseries endpoint.

The portal is unofficial and may change without notice. Credentials, cookies
and URLs carrying the contract account number are never logged.
"""

from __future__ import annotations

import gzip
import html
import http.client
import http.cookiejar
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import maintenance

_LOGGER = logging.getLogger(__name__)

MYT = timezone(timedelta(hours=8), "MYT")

LOGIN_URL = "https://www.mytnb.com.my/api/sitecore/Account/Login"
SSO_URL = "https://myaccount.mytnb.com.my/SSO/SSOHandler"
ACCOUNT_BASE = "https://myaccount.mytnb.com.my"
SMARTLIVING_BASE = "https://smartliving.myaccount.mytnb.com.my"
DASHBOARD_URL = f"{SMARTLIVING_BASE}/dashboard"
TIMESERIES_URL = f"{SMARTLIVING_BASE}/my_energy_request/timeseries"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# The smartliving WAF rejects requests without a browser-like header set.
HTML_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"
    ),
}

SMARTMETER_PATH = "/AccountManagement/SmartMeter/Index/TRIL?caNo={ca_no}"
# The bill: the account dashboard shows one account at a time (the one picked in its account list).
BILL_DASHBOARD_URL = f"{ACCOUNT_BASE}/AccountManagement/IndividualDashboard"
SELECT_ACCOUNT_URL = f"{BILL_DASHBOARD_URL}/CheckIsPrepaymentCA?selectedAccountID={{account_id}}"
ACCOUNT_ITEM_RE = re.compile(
    r'<li\s+id="([0-9a-fA-F-]{36})"\s+class="[^"]*\baccountListItem\b[^"]*"[^>]*>(.*?)</li>', re.DOTALL
)
ACCOUNT_ITEM_NUMBER_RE = re.compile(r"-\s*(\d{10,14})\s*<")
SELECTED_ACCOUNT_RE = re.compile(r"selectedAccountNo=(\d{10,14})")
# "My Amount Due" / "I've cleared all bills" / "I've paid extra", then the big figure and maybe "Due Date: 23-Nov-2026".
BILL_HEADLINE_RE = re.compile(r'class="card-text-20-14 darken"[^>]*>\s*(.*?)\s*</span>', re.DOTALL)
BILL_AMOUNT_RE = re.compile(r'class="card-text-48-24-black"[^>]*>\s*([\d,]+(?:\.\d{1,2})?)\s*</span>')
BILL_DUE_DATE_RE = re.compile(r"Due Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")
BILL_SECTION_HEADLINES = {"billing summary", "payment history"}
BILL_CREDIT_WORDS = ("paid extra", "credit", "lebihan")
# Links the portal itself renders (e.g. "View Energy Usage"); caNo may be encoded.
SMARTMETER_LINK_RE = re.compile(r"/AccountManagement/SmartMeter/Index/TRIL\?caNo=([^\"'\s<>&#]+)")
FORM_FIELD_RE = re.compile(r'name="([^"]+)" value="([^"]*)"')
SDPUDCID_RE = re.compile(r'"sdpudcid":"(\d+)"')
POINT_DATETIME_FORMAT = "%Y-%m-%d %H:%M"
REQUEST_DATE_FORMAT = "%Y-%m-%d+00:00"

# The "Loading Smart Meter" page auto-submits a hidden form (USERTOKEN, CA_NO)
# to smartliving's /login?renew; that POST is what creates the smartliving session.
FORM_RE = re.compile(r"<form\b([^>]*)>", re.IGNORECASE)
INPUT_RE = re.compile(r"<input\b([^>]*)>", re.IGNORECASE)
ATTR_RE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*"([^"]*)"')
HANDOFF_HOST_SUFFIX = ".mytnb.com.my"
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
LONG_NUMBER_RE = re.compile(r"\d{6,}")
FIELD_NAME_RE = re.compile(r'name="([^"]+)"')
LOG_TITLE_CHARS = 80
LOG_LIST_ITEMS = 12

HTTP_TIMEOUT = 45
MAX_REDIRECTS = 30
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SESSION_LAG_RETRIES = 3
SESSION_LAG_DELAY = 2.0
HTTP_OK = 200
AUTH_STATUSES = {401, 403}


class MyTnbError(Exception):
    """Base error for the portal client."""


class AuthError(MyTnbError):
    """Credentials rejected, or the session is missing/expired."""


class CredentialsError(AuthError):
    """myTNB rejected the email or password (retrying soon could lock the account)."""


class PortalError(MyTnbError):
    """Transient network or server-side failure."""


class MaintenanceError(PortalError):
    """myTNB answered with a maintenance page or HTTP 503: back off, don't alert."""


class MeterNotFoundError(MyTnbError):
    """Logged in, but the portal did not bind a smart meter to this account."""


@dataclass(frozen=True, slots=True)
class EnergyPoint:
    """One metered interval (start is timezone-aware, MYT)."""

    start: datetime
    value: float


@dataclass(frozen=True, slots=True)
class BillInfo:
    """What the account page says is owed (amount in RM; a credit is negative)."""

    amount_due: float
    due_date: date | None = None


@dataclass(slots=True)
class PortalData:
    """Usage (kWh) and cost (RM) timeseries for one meter, plus the bill when the account page shows it."""

    usage: list[EnergyPoint]
    cost: list[EnergyPoint]
    bill: BillInfo | None = None


@dataclass(slots=True)
class _Response:
    status: int
    body: str
    content_type: str
    url: str = ""


def set_debug(enabled: bool) -> None:
    """Switch the step-by-step login trace on or off (``debug_login`` option)."""

    _LOGGER.setLevel(logging.DEBUG if enabled else logging.NOTSET)


def _debugging() -> bool:
    return _LOGGER.isEnabledFor(logging.DEBUG)


def _scrub(text: str) -> str:
    """Collapse whitespace, mask long digit runs (account numbers) and shorten."""

    text = LONG_NUMBER_RE.sub("#", " ".join(text.split()))
    return text if len(text) <= LOG_TITLE_CHARS else text[: LOG_TITLE_CHARS - 1] + "…"


def _mask(value: str) -> str:
    """Last 4 characters only, so a pasted log does not reveal the account."""

    return f"••••{value[-4:]}"


def _names(values: object) -> str:
    items = sorted({_scrub(str(v)) for v in values})  # type: ignore[attr-defined]
    more = f" +{len(items) - LOG_LIST_ITEMS}" if len(items) > LOG_LIST_ITEMS else ""
    return ", ".join(items[:LOG_LIST_ITEMS]) + more if items else "-"


def describe_page(response: _Response) -> str:
    """A secret-free summary of a portal response for the debug trace.

    Only names and counts: no cookie values, form values, query strings,
    account numbers or page text beyond a scrubbed <title>.
    """

    body = response.body
    parts = [f"HTTP {response.status}", f"at {_where(response.url)}", f"{len(body)} chars"]
    if response.content_type:
        parts.append(response.content_type.split(";")[0])
    if title := TITLE_RE.search(body):
        parts.append(f"title '{_scrub(html.unescape(title.group(1)))}'")
    if "json" in response.content_type or body.lstrip().startswith("{"):
        try:
            payload = json.loads(body)
        except ValueError:
            parts.append("JSON unreadable")
        else:
            if isinstance(payload, dict):
                parts.append(f"JSON keys [{_names(payload)}]")
                if "redirect" in payload:
                    parts.append(f"redirect={bool(payload.get('redirect'))}")
    else:
        fields = FIELD_NAME_RE.findall(body)
        if fields:
            parts.append(f"form fields [{_names(fields)}]")
        password_box = 'type="password"' in body.lower()
        parts.append(f"password box {'yes' if password_box else 'no'}")
        parts.append(f"smart meter links {len(SMARTMETER_LINK_RE.findall(html.unescape(body)))}")
        parts.append(f"sdpudcid {'yes' if SDPUDCID_RE.search(body) else 'no'}")
    return ", ".join(parts)


class _KeepAliveClient:
    """HTTPS with one persistent connection per host, manual redirects and a cookie jar.

    smartliving keeps its session per backend and rebuilds it through redirect
    chains (/login -> /login -> /dashboard -> /login ...) of up to ~20 hops.
    urllib opens a new connection per request, so hops land on other backends
    and the chain never settles (and urllib calls >4 repeats of a URL a loop).
    Reusing the connection, as a browser does, keeps the session together.
    """

    def __init__(self, jar: http.cookiejar.CookieJar) -> None:
        self._jar = jar
        self._conns: dict[str, http.client.HTTPSConnection] = {}

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    def _exchange(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[http.client.HTTPResponse, bytes]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise PortalError("Portal redirected to a non-https address")
        request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - https only; cookie bookkeeping
        self._jar.add_cookie_header(request)
        target = parts.path or "/"
        if parts.query:
            target += f"?{parts.query}"
        # Send headers exactly as given, in order, with browser casing: urllib's
        # Request rewrites names (User-agent, Dnt) and Cloudflare 403s that.
        cookie = request.get_header("Cookie") or request.unredirected_hdrs.get("Cookie")
        ordered = {"User-Agent": USER_AGENT, **headers, "Connection": "keep-alive"}
        if cookie:
            ordered["Cookie"] = cookie
        for attempt in (1, 2):  # a kept-alive connection may have been closed by the server
            conn = self._conns.get(parts.netloc)
            if conn is None:
                conn = self._conns[parts.netloc] = http.client.HTTPSConnection(parts.netloc, timeout=HTTP_TIMEOUT)
            try:
                conn.putrequest(method, target, skip_accept_encoding=True)
                for name, value in ordered.items():
                    conn.putheader(name, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                response = conn.getresponse()
                raw = response.read()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                del self._conns[parts.netloc]
                if attempt == 2:  # noqa: PLR2004 - second try on a fresh connection
                    raise
                continue
            self._jar.extract_cookies(response, request)  # type: ignore[arg-type]
            return response, raw
        raise AssertionError("unreachable")

    def open(self, url: str, body: bytes | None, headers: dict[str, str]) -> _Response:
        method = "POST" if body is not None else "GET"
        for _ in range(MAX_REDIRECTS + 1):
            response, raw = self._exchange(method, url, body, headers)
            location = response.getheader("Location")
            if response.status in REDIRECT_STATUSES and location:
                next_url = urllib.parse.urljoin(url, location)
                if _debugging():
                    _LOGGER.debug("  redirect %d: %s -> %s", response.status, _where(url), _where(next_url))
                if response.status in (301, 302, 303):  # browsers turn these into a GET
                    method, body = "GET", None
                    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                url = next_url
                continue
            return _Response(
                status=response.status,
                body=_decode(raw, response.getheader("Content-Encoding")),
                content_type=response.getheader("Content-Type", ""),
                url=url,
            )
        raise AuthError("Portal is stuck in a login redirect loop")


def _attrs(tag: str) -> dict[str, str]:
    return {name.lower(): html.unescape(value) for name, value in ATTR_RE.findall(tag)}


def find_handoff_form(body: str) -> tuple[str, dict[str, str]] | None:
    """The smartliving hand-off form on the smart meter page: (action URL, hidden fields).

    Only an https action on a myTNB host is accepted, so the session token is
    never posted anywhere else.
    """

    for form in FORM_RE.findall(body):
        action = _attrs(form).get("action", "")
        parts = urllib.parse.urlsplit(action)
        host = parts.hostname or ""
        if parts.scheme == "https" and host.startswith("smartliving.") and host.endswith(HANDOFF_HOST_SUFFIX):
            fields = {}
            for tag in INPUT_RE.findall(body):
                attrs = _attrs(tag)
                if attrs.get("name") and attrs.get("type", "").lower() == "hidden":
                    fields[attrs["name"]] = attrs.get("value", "")
            return action, fields
    return None


def _where(url: str) -> str:
    """Host and path of ``url`` only: queries carry the account number."""

    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}" or "?"


def find_smartmeter_links(body: str) -> list[str]:
    """Distinct caNo values of the smart meter links in a portal page."""

    seen: dict[str, None] = {}
    for value in SMARTMETER_LINK_RE.findall(html.unescape(body)):
        seen.setdefault(value, None)
    return list(seen)


def pick_smartmeter_ca(configured: str, links: list[str]) -> str | None:
    """The portal's own caNo spelling of ``configured``, if it lists that account.

    Never guesses from a single link: with several accounts on one login that
    could bind this account to another account's meter.
    """

    for value in links:
        if urllib.parse.unquote(value) == configured:
            return value
    return None


def _decode(raw: bytes, encoding: str | None) -> str:
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def parse_points(payload: object) -> list[EnergyPoint]:
    """Extract sorted points from a timeseries payload, skipping nulls."""

    points: list[EnergyPoint] = []
    inner = payload.get("data") if isinstance(payload, dict) else None
    series = inner.get("timeseries", []) if isinstance(inner, dict) else []
    for item in series if isinstance(series, list) else []:
        if not isinstance(item, dict):
            continue
        for point in item.get("data") or []:
            if not isinstance(point, dict) or point.get("value") is None or not point.get("datetime"):
                continue
            try:
                start = datetime.strptime(str(point["datetime"]), POINT_DATETIME_FORMAT).replace(tzinfo=MYT)
                points.append(EnergyPoint(start=start, value=float(point["value"])))
            except (TypeError, ValueError):
                continue
    points.sort(key=lambda p: p.start)
    return points


def parse_bill(body: str) -> BillInfo | None:
    """The amount due (a credit is negative) and due date from the account dashboard, or None.

    The bill card leads with "My Amount Due", "I've cleared all bills" or
    "I've paid extra", followed by the large figure and, when something is
    owed, "Due Date: 23-Nov-2026".
    """

    for headline in BILL_HEADLINE_RE.finditer(body):
        title = html.unescape(" ".join(headline.group(1).split())).lower()
        if title in BILL_SECTION_HEADLINES:
            continue
        following = body[headline.end() :]
        nxt = BILL_HEADLINE_RE.search(following)
        card = following[: nxt.start()] if nxt else following
        amount_match = BILL_AMOUNT_RE.search(card)
        if amount_match is None:
            continue
        amount = float(amount_match.group(1).replace(",", ""))
        if any(word in title for word in BILL_CREDIT_WORDS):
            amount = -amount
        due = None
        if amount > 0 and (due_match := BILL_DUE_DATE_RE.search(card)):
            try:
                due = datetime.strptime(due_match.group(1), "%d-%b-%Y").date()
            except ValueError:
                due = None
        return BillInfo(amount_due=amount, due_date=due)
    return None


def parse_account_list(body: str) -> dict[str, str]:
    """Account number -> the dashboard's account id (GUID), from its account switcher."""

    accounts: dict[str, str] = {}
    for account_id, inner in ACCOUNT_ITEM_RE.findall(body):
        if number := ACCOUNT_ITEM_NUMBER_RE.search(inner):
            accounts.setdefault(number.group(1), account_id)
    return accounts


def selected_account(body: str) -> str | None:
    """The account number the dashboard is showing."""

    match = SELECTED_ACCOUNT_RE.search(body)
    return match.group(1) if match else None


class MyTnbSession:
    """Logged-in portal session for one TNB account number (own cookie jar).

    Several accounts can share one myTNB login; each still gets its own session
    so the smartliving dashboard is always bound to the right meter.
    """

    def __init__(self, username: str, password: str, ca_no: str, bill_no: str = "") -> None:
        self._username = username
        self._password = password
        self.ca_no = ca_no
        self._smartmeter_path = SMARTMETER_PATH.format(ca_no=ca_no)
        self._jar = http.cookiejar.CookieJar()
        self._client = _KeepAliveClient(self._jar)
        self.sdpudcid: str | None = None
        # The printed account number picks this account on the bill dashboard (ca_no may be an encoded token).
        self.bill_no = bill_no or (ca_no if ca_no.isdigit() else "")
        self._bill_warned = False

    # -- HTTP -----------------------------------------------------------------
    def _send(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        step: str = "request",
    ) -> _Response:
        if not _debugging():
            return self._send_raw(url, data=data, headers=headers)
        method = "POST" if data is not None else "GET"
        _LOGGER.debug("[%s] %s %s", step, method, _where(url))
        try:
            response = self._send_raw(url, data=data, headers=headers)
        except MyTnbError as err:
            _LOGGER.debug("[%s] failed: %s (cause: %s)", step, err, type(err.__cause__).__name__)
            raise
        _LOGGER.debug("[%s] %s", step, describe_page(response))
        _LOGGER.debug("[%s] cookies %s", step, self._cookie_names())
        return response

    def _cookie_names(self) -> str:
        """Cookie names per domain (never values)."""

        by_domain: dict[str, list[str]] = {}
        for cookie in self._jar:
            by_domain.setdefault(cookie.domain.lstrip("."), []).append(cookie.name)
        return "; ".join(f"{domain}: {_names(names)}" for domain, names in sorted(by_domain.items())) or "none"

    def _send_raw(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> _Response:
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        all_headers = {"Accept-Encoding": "gzip, deflate", **(headers or {})}
        if body is not None:
            all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        try:
            response = self._client.open(url, body, all_headers)
        except (http.client.HTTPException, OSError, EOFError, zlib.error) as err:
            raise PortalError(f"Portal request failed: {type(err).__name__}") from err
        title_match = TITLE_RE.search(response.body) if "html" in response.content_type else None
        if maintenance.looks_like_maintenance(
            response.status,
            response.body if "html" in response.content_type else "",
            html.unescape(title_match.group(1)) if title_match else "",
        ):
            raise MaintenanceError(f"myTNB is under maintenance ({_where(response.url)} HTTP {response.status})")
        if response.status in AUTH_STATUSES:
            return _Response(status=response.status, body="", content_type="", url=response.url)
        if response.status >= 400:  # noqa: PLR2004 - HTTP error range
            raise PortalError(f"Portal returned HTTP {response.status}")
        return response

    # -- Login ----------------------------------------------------------------
    def login(self) -> str:
        """Run the full login flow and return the meter id (sdpudcid)."""

        self._jar.clear()
        self._client.close()  # fresh connections for a fresh login
        self.sdpudcid = None

        _LOGGER.debug("Login trace for account %s (debug_login is on)", _mask(self.ca_no))
        response = self._send(LOGIN_URL, data={"Email": self._username, "Password": self._password}, step="login")
        fields = {name: html.unescape(value) for name, value in FORM_FIELD_RE.findall(response.body)}
        if not fields:
            raise CredentialsError("Login returned no SSO form fields (wrong email or password?)")

        _LOGGER.debug("[login] %d SSO form field(s) found", len(fields))
        response = self._send(SSO_URL, data=fields, step="sso")
        if response.status != HTTP_OK:
            raise CredentialsError(f"SSO handler rejected the login (HTTP {response.status})")

        # Use the portal's own spelling of this account's link when it lists it;
        # the count and match also explain a missing meter id below.
        links = find_smartmeter_links(response.body)
        portal_ca = pick_smartmeter_ca(self.ca_no, links)
        _LOGGER.debug(
            "Account page %s lists %d smart meter link(s); this account %s",
            _where(response.url),
            len(links),
            "is one of them" if portal_ca else "is not among them",
        )
        smartmeter_path = SMARTMETER_PATH.format(ca_no=portal_ca) if portal_ca else self._smartmeter_path

        response = self._send(f"{ACCOUNT_BASE}{smartmeter_path}", step="smart meter page")
        if response.status != HTTP_OK:
            raise PortalError(f"Smart meter page returned HTTP {response.status}")
        if "/Account/Login" in response.url:
            raise MeterNotFoundError(
                f"Smart meter page sent us back to the login page ({_where(response.url)}); "
                "the session was not accepted"
            )

        handoff = find_handoff_form(response.body)
        if handoff is None:
            _LOGGER.debug("[smart meter page] no smartliving hand-off form; relying on the dashboard redirect")
        else:
            action, handoff_fields = handoff
            _LOGGER.debug("[handoff] %d hidden field(s): %s", len(handoff_fields), _names(handoff_fields))
            try:
                response = self._send(action, data=handoff_fields, headers=HTML_HEADERS, step="handoff")
            except AuthError:
                _LOGGER.debug("[handoff] redirect loop; trying the dashboard anyway")
            else:
                if match := SDPUDCID_RE.search(response.body):
                    self.sdpudcid = match.group(1)
                    return self.sdpudcid

        self.sdpudcid = self._fetch_sdpudcid(len(links), matched=portal_ca is not None)
        return self.sdpudcid

    def _fetch_sdpudcid(self, links_seen: int, *, matched: bool) -> str:
        landed = "?"
        for attempt in range(SESSION_LAG_RETRIES):
            try:
                response = self._send(DASHBOARD_URL, headers=HTML_HEADERS, step=f"dashboard {attempt + 1}")
                landed = f"{_where(response.url)} HTTP {response.status}"
                text = "" if response.status in AUTH_STATUSES else response.body
            except AuthError:
                landed = "a redirect loop"
                text = ""  # transient redirect loop while the new session propagates
            if match := SDPUDCID_RE.search(text):
                return match.group(1)
            if attempt < SESSION_LAG_RETRIES - 1:
                time.sleep(SESSION_LAG_DELAY)
        found = "including this account" if matched else "none with this account number"
        raise MeterNotFoundError(
            f"Logged in, but the smartliving dashboard ({landed}) returned no meter id "
            f"({links_seen} smart meter link(s) on the account page, {found}); "
            "open View Energy Usage for this account in a browser and paste that link as smartmeter_url"
        )

    # -- Data -----------------------------------------------------------------
    def fetch(self, start: datetime, end: datetime) -> PortalData:
        """Fetch usage and cost, logging in (again) as needed."""

        if self.sdpudcid is None:
            self.login()
        try:
            data = self._fetch_all(start, end)
        except AuthError:
            _LOGGER.info("Portal session expired; logging in again")
            self.login()
            data = self._fetch_all(start, end)
        data.bill = self.fetch_bill()
        return data

    def fetch_bill(self) -> BillInfo | None:
        """The amount due from the account dashboard (None when unreadable; never fails the sync)."""

        try:
            page = self.bill_dashboard()
        except MyTnbError as err:
            _LOGGER.warning("Could not load the myTNB account dashboard for the bill: %s", err)
            return None
        if page is None:
            return None
        bill = parse_bill(page)
        if bill is None:
            _LOGGER.warning("The myTNB dashboard shows no amount due for account %s", _mask(self.bill_no))
        return bill

    def _warn_once(self, message: str) -> None:
        if not self._bill_warned:
            self._bill_warned = True
            _LOGGER.warning(message)
        else:
            _LOGGER.debug(message)

    def _get_dashboard(self, step: str) -> str:
        response = self._send(BILL_DASHBOARD_URL, headers=HTML_HEADERS, step=step)
        if response.status != HTTP_OK or "/Account/Login" in response.url:
            raise PortalError(f"Account dashboard returned HTTP {response.status} at {_where(response.url)}")
        return response.body

    def bill_dashboard(self) -> str | None:
        """The account dashboard showing this account, switching to it in the account list if needed."""

        body = self._get_dashboard("bill dashboard")
        accounts = parse_account_list(body)
        target = self.bill_no
        if not target:
            if len(accounts) <= 1:
                return body  # one account on this login: it is the one shown
            self._warn_once(
                "This myTNB login has several accounts; add account_number (the number on the bill) "
                "to read this account's amount due"
            )
            return None
        if selected_account(body) == target:
            return body
        return self._switch_dashboard(target, accounts)

    def _switch_dashboard(self, target: str, accounts: dict[str, str]) -> str | None:
        """Pick ``target`` in the dashboard's account list, then reload the dashboard."""

        account_id = accounts.get(target)
        if account_id is None:
            self._warn_once(f"Account {_mask(target)} is not in the myTNB dashboard's account list; no amount due")
            return None
        headers = {**HTML_HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        choice = self._send(SELECT_ACCOUNT_URL.format(account_id=account_id), headers=headers, step="select account")
        if choice.body.strip().lower() == "true":
            self._warn_once(f"Account {_mask(target)} is a prepaid account; there is no bill to read")
            return None
        body = self._get_dashboard("bill dashboard (switched)")
        if selected_account(body) != target:
            self._warn_once(f"The myTNB dashboard did not switch to account {_mask(target)}; no amount due")
            return None
        return body

    def _fetch_all(self, start: datetime, end: datetime) -> PortalData:
        errors: list[PortalError] = []

        def one(metric: str, granularity: str | None) -> list[EnergyPoint]:
            try:
                return self._fetch_metric(metric, granularity, start, end)
            except PortalError as err:
                _LOGGER.warning("Fetching portal %s failed: %s", metric, err)
                errors.append(err)
                return []

        usage = one("usage", "HOUR")
        cost = one("cost", None)  # cost does not support sub-daily granularity
        if len(errors) == 2:  # noqa: PLR2004 - both metrics failed
            raise errors[0]
        return PortalData(usage=usage, cost=cost)

    def _fetch_metric(self, metric: str, granularity: str | None, start: datetime, end: datetime) -> list[EnergyPoint]:
        if self.sdpudcid is None:
            raise AuthError("Not logged in")
        commodity_url = f"{SMARTLIVING_BASE}/commodity/electric/{metric}"
        headers = {
            **HTML_HEADERS,
            "Referer": commodity_url,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "X-Requested-With": "XMLHttpRequest",
            "X-Request": "JSON",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        params = {
            "metric": metric,
            "view": "BILL",
            "sdpudcid": self.sdpudcid,
            "start": start.astimezone(MYT).strftime(REQUEST_DATE_FORMAT),
            "end": end.astimezone(MYT).strftime(REQUEST_DATE_FORMAT),
        }
        if granularity:
            params["granularity"] = granularity
        url = f"{TIMESERIES_URL}?{urllib.parse.urlencode(params)}"

        for attempt in range(SESSION_LAG_RETRIES):
            last = attempt == SESSION_LAG_RETRIES - 1
            try:
                armed = self._send(commodity_url, headers=HTML_HEADERS, step=f"{metric} page {attempt + 1}")
            except AuthError:
                if last:
                    raise
                time.sleep(SESSION_LAG_DELAY)
                continue
            if armed.status in AUTH_STATUSES:
                raise AuthError(f"Commodity page rejected (HTTP {armed.status})")

            response = self._send(url, headers=headers, step=f"{metric} data {attempt + 1}")
            if response.status in AUTH_STATUSES:
                raise AuthError(f"Timeseries request rejected (HTTP {response.status})")
            try:
                payload = json.loads(response.body)
            except ValueError as err:
                # An expired session is served the HTML login page with HTTP 200.
                raise AuthError("Timeseries endpoint returned non-JSON") from err

            if not (isinstance(payload, dict) and payload.get("redirect")):
                points = parse_points(payload)
                _LOGGER.debug("Portal %s: %d point(s)", metric, len(points))
                return points
            if not last:
                time.sleep(SESSION_LAG_DELAY)
        raise AuthError(f"Timeseries {metric} kept redirecting to login")


def _cli() -> None:
    """Smoke test: ``MYTNB_PASSWORD=... [MYTNB_DEBUG=1] python3 mytnb.py <email> <account number>``."""

    import getpass  # noqa: PLC0415 - CLI only
    import os  # noqa: PLC0415 - CLI only
    import sys  # noqa: PLC0415 - CLI only

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    set_debug(os.environ.get("MYTNB_DEBUG", "") not in ("", "0"))
    if len(sys.argv) != 3:  # noqa: PLR2004 - argv layout
        _LOGGER.error("usage: MYTNB_PASSWORD=... python3 mytnb.py <email> <account number>")
        raise SystemExit(2)
    password = os.environ.get("MYTNB_PASSWORD") or getpass.getpass("myTNB password: ")
    session = MyTnbSession(sys.argv[1], password, sys.argv[2])
    session.login()
    _LOGGER.info("Logged in; meter id found (%d digits)", len(session.sdpudcid or ""))
    now = datetime.now(MYT)
    data = session.fetch(now - timedelta(days=7), now + timedelta(days=1))
    for name, points in (("usage", data.usage), ("cost", data.cost)):
        if points:
            _LOGGER.info(
                "%s: %d point(s) %s .. %s, total %.3f",
                name,
                len(points),
                points[0].start.isoformat(),
                points[-1].start.isoformat(),
                sum(p.value for p in points),
            )
        else:
            _LOGGER.info("%s: no points", name)
    if data.bill is None:
        _LOGGER.info("bill: not readable")
    else:
        _LOGGER.info("bill: RM %.2f due %s", data.bill.amount_due, data.bill.due_date or "-")


if __name__ == "__main__":
    _cli()
