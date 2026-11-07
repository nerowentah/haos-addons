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
import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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

SMARTMETER_PATH_RE = re.compile(r"/AccountManagement/SmartMeter/Index/TRIL\?caNo=[^\"\s]+")
FORM_FIELD_RE = re.compile(r'name="([^"]+)" value="([^"]*)"')
SDPUDCID_RE = re.compile(r'"sdpudcid":"(\d+)"')
POINT_DATETIME_FORMAT = "%Y-%m-%d %H:%M"
REQUEST_DATE_FORMAT = "%Y-%m-%d+00:00"

HTTP_TIMEOUT = 45
SESSION_LAG_RETRIES = 3
SESSION_LAG_DELAY = 2.0
HTTP_OK = 200
AUTH_STATUSES = {401, 403}


class MyTnbError(Exception):
    """Base error for the portal client."""


class AuthError(MyTnbError):
    """Credentials rejected, or the session is missing/expired."""


class PortalError(MyTnbError):
    """Transient network or server-side failure."""


@dataclass(frozen=True, slots=True)
class EnergyPoint:
    """One metered interval (start is timezone-aware, MYT)."""

    start: datetime
    value: float


@dataclass(slots=True)
class PortalData:
    """Usage (kWh) and cost (RM) timeseries for one meter."""

    usage: list[EnergyPoint]
    cost: list[EnergyPoint]


@dataclass(slots=True)
class _Response:
    status: int
    body: str
    content_type: str


def extract_smartmeter_path(smartmeter_url: str) -> str:
    """Extract the SmartMeter/Index/TRIL?caNo=... path from the pasted URL."""

    match = SMARTMETER_PATH_RE.search(smartmeter_url)
    if not match:
        raise MyTnbError("Could not find a SmartMeter/Index/TRIL?caNo=... path in the URL")
    return match.group(0)


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


class MyTnbSession:
    """Logged-in portal session for one account (own cookie jar)."""

    def __init__(self, username: str, password: str, smartmeter_url: str) -> None:
        self._username = username
        self._password = password
        self._smartmeter_path = extract_smartmeter_path(smartmeter_url)
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self.sdpudcid: str | None = None

    # -- HTTP -----------------------------------------------------------------
    def _send(
        self, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> _Response:
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(  # noqa: S310 - fixed https myTNB endpoints
            url,
            data=body,
            method="POST" if body is not None else "GET",
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate", **(headers or {})},
        )
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT) as response:
                return _Response(
                    status=response.status,
                    body=_decode(response.read(), response.headers.get("Content-Encoding")),
                    content_type=response.headers.get("Content-Type", ""),
                )
        except urllib.error.HTTPError as err:
            if err.code in AUTH_STATUSES:
                return _Response(status=err.code, body="", content_type="")
            # 3xx here means a redirect loop (urllib gave up following it).
            if 300 <= err.code < 400:  # noqa: PLR2004 - HTTP redirect range
                raise AuthError("Portal is stuck in a login redirect loop") from err
            raise PortalError(f"Portal returned HTTP {err.code}") from err
        except (urllib.error.URLError, OSError, EOFError, zlib.error) as err:
            raise PortalError(f"Portal request failed: {type(err).__name__}") from err

    # -- Login ----------------------------------------------------------------
    def login(self) -> str:
        """Run the full login flow and return the meter id (sdpudcid)."""

        self._jar.clear()
        self.sdpudcid = None

        response = self._send(LOGIN_URL, data={"Email": self._username, "Password": self._password})
        fields = {name: html.unescape(value) for name, value in FORM_FIELD_RE.findall(response.body)}
        if not fields:
            raise AuthError("Login returned no SSO form fields (wrong email or password?)")

        response = self._send(SSO_URL, data=fields)
        if response.status != HTTP_OK:
            raise AuthError(f"SSO handler rejected the login (HTTP {response.status})")

        response = self._send(f"{ACCOUNT_BASE}{self._smartmeter_path}")
        if response.status != HTTP_OK:
            raise PortalError(f"Smart meter page returned HTTP {response.status}")

        self.sdpudcid = self._fetch_sdpudcid()
        return self.sdpudcid

    def _fetch_sdpudcid(self) -> str:
        for attempt in range(SESSION_LAG_RETRIES):
            try:
                response = self._send(DASHBOARD_URL, headers=HTML_HEADERS)
                text = "" if response.status in AUTH_STATUSES else response.body
            except AuthError:
                text = ""  # transient redirect loop while the new session propagates
            if match := SDPUDCID_RE.search(text):
                return match.group(1)
            if attempt < SESSION_LAG_RETRIES - 1:
                time.sleep(SESSION_LAG_DELAY)
        raise AuthError("Smartliving dashboard did not return a meter id (is smart meter access enabled?)")

    # -- Data -----------------------------------------------------------------
    def fetch(self, start: datetime, end: datetime) -> PortalData:
        """Fetch usage and cost, logging in (again) as needed."""

        if self.sdpudcid is None:
            self.login()
        try:
            return self._fetch_all(start, end)
        except AuthError:
            _LOGGER.info("Portal session expired; logging in again")
            self.login()
            return self._fetch_all(start, end)

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
                armed = self._send(commodity_url, headers=HTML_HEADERS)
            except AuthError:
                if last:
                    raise
                time.sleep(SESSION_LAG_DELAY)
                continue
            if armed.status in AUTH_STATUSES:
                raise AuthError(f"Commodity page rejected (HTTP {armed.status})")

            response = self._send(url, headers=headers)
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
    """Smoke test: ``MYTNB_PASSWORD=... python3 -I mytnb.py <email> '<smartmeter url>'``."""

    import getpass  # noqa: PLC0415 - CLI only
    import os  # noqa: PLC0415 - CLI only
    import sys  # noqa: PLC0415 - CLI only

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    if len(sys.argv) != 3:  # noqa: PLR2004 - argv layout
        _LOGGER.error("usage: MYTNB_PASSWORD=... python3 -I mytnb.py <email> '<smartmeter url>'")
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


if __name__ == "__main__":
    _cli()
