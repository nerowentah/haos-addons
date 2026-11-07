"""Public holidays that count as off-peak for the TNB Time-of-Use tariff."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

_LOGGER = logging.getLogger(__name__)

CALENDARIFIC_URL = "https://calendarific.com/api/v2/holidays"
HTTP_TIMEOUT = 20
FIRST_MONDAY_OF_JUNE_WEEKDAY = 0


def fixed_holidays(year: int) -> set[date]:
    """National holidays on fixed Gregorian dates (lunar ones need Calendarific/manual)."""

    june_first = date(year, 6, 1)
    agong_birthday = june_first + timedelta(days=(FIRST_MONDAY_OF_JUNE_WEEKDAY - june_first.weekday()) % 7)
    return {
        date(year, 1, 1),  # New Year's Day (TNB treats it as off-peak nationwide)
        date(year, 5, 1),  # Labour Day
        agong_birthday,  # Agong's Birthday, first Monday of June
        date(year, 8, 31),  # National Day
        date(year, 9, 16),  # Malaysia Day
        date(year, 12, 25),  # Christmas
    }


def fetch_calendarific(api_key: str, year: int, country: str = "MY") -> set[date]:
    """Fetch national holidays for a year from Calendarific (empty set on failure)."""

    query = urllib.parse.urlencode({"api_key": api_key, "country": country, "year": year, "type": "national"})
    try:
        with urllib.request.urlopen(f"{CALENDARIFIC_URL}?{query}", timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - fixed https URL
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        # The URL carries the API key, so only log the year.
        _LOGGER.warning("Could not fetch Calendarific holidays for %s", year)
        return set()

    result: set[date] = set()
    holidays = (payload.get("response") or {}).get("holidays") if isinstance(payload, dict) else None
    for item in holidays or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        # TNB only recognises the first day of Hari Raya Haji.
        if "haji" in name and "day 2" in name:
            continue
        iso = str(((item.get("date") or {}).get("iso")) or "")[:10]
        try:
            result.add(date.fromisoformat(iso))
        except ValueError:
            continue
    return result


class HolidayCalendar:
    """Merges fixed, manual and (optionally) Calendarific holidays."""

    def __init__(self, extra: list[date], api_key: str = "") -> None:
        self._extra = set(extra)
        self._api_key = api_key
        self._fetched: dict[int, set[date]] = {}

    def refresh(self, year: int) -> None:
        """(Re)load Calendarific holidays for a year when an API key is set."""

        if not self._api_key:
            return
        fetched = fetch_calendarific(self._api_key, year)
        if fetched:
            self._fetched[year] = fetched
            _LOGGER.info("Loaded %d Calendarific holiday(s) for %s", len(fetched), year)

    def is_holiday(self, day: date) -> bool:
        """Return whether ``day`` is an off-peak public holiday."""

        return day in self._extra or day in fixed_holidays(day.year) or day in self._fetched.get(day.year, set())
