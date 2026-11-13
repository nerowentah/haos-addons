"""Vendor maintenance windows and back-off (shared by the apps; keep the copies identical).

A window is one line such as ``daily 00:00-06:00``, ``sun 01:00-05:00``,
``mon-fri 23:00-02:00`` (crosses midnight), ``weekends 02:00-04:00`` or
``mon,wed 00:00-24:00``. A line without a day part means every day. Times are
read in the timezone of the ``datetime`` passed in.

Inside a window an app makes no calls to the vendor. When the vendor still
answers with a maintenance page or HTTP 503, ``Backoff`` spaces out retries.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime

DEFAULT_WINDOWS = ("daily 00:00-06:00",)
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_GROUPS = {
    "daily": frozenset(range(7)),
    "everyday": frozenset(range(7)),
    "weekdays": frozenset(range(5)),
    "weekends": frozenset({5, 6}),
}
WINDOW_RE = re.compile(r"^(?:(?P<days>[a-z,\-]+)\s+)?(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})$")
MAINTENANCE_TEXT_RE = re.compile(
    r"under\s+maintenance|scheduled\s+maintenance|system\s+maintenance|maintenance\s+(?:mode|in\s+progress)"
    r"|temporarily\s+unavailable|sedang\s+(?:dalam\s+)?(?:penyelenggaraan|diselenggara)",
    re.IGNORECASE,
)
MAINTENANCE_STATUSES = frozenset({503})
SHORT_PAGE_CHARS = 20_000  # real app pages are far longer; maintenance pages are short
END_OF_DAY = "24:00"
HOURS_PER_DAY = 24
MAX_CHAINED_WINDOWS = 14


class MaintenanceConfigError(ValueError):
    """A maintenance window line could not be understood."""


def _clock(text: str, *, allow_end_of_day: bool) -> dtime | None:
    """'06:00' -> time(6); '24:00' -> None (end of day) when allowed."""

    if allow_end_of_day and text == END_OF_DAY:
        return None
    hour, minute = (int(part) for part in text.split(":"))
    if not (0 <= hour < HOURS_PER_DAY and 0 <= minute < 60):  # noqa: PLR2004 - minutes per hour
        raise MaintenanceConfigError(f"'{text}' is not a valid time (use HH:MM, 00:00-23:59)")
    return dtime(hour, minute)


def _days(text: str | None) -> frozenset[int]:
    if not text:
        return DAY_GROUPS["daily"]
    if text in DAY_GROUPS:
        return DAY_GROUPS[text]
    days: set[int] = set()
    for part in text.split(","):
        first, _, last = part.partition("-")
        if first not in DAY_NAMES or (last and last not in DAY_NAMES):
            raise MaintenanceConfigError(
                f"'{part}' is not a day; use daily, weekdays, weekends, mon..sun, mon-fri or mon,wed"
            )
        start = DAY_NAMES.index(first)
        end = DAY_NAMES.index(last) if last else start
        index = start
        while True:  # mon-fri, and wrapping ranges such as fri-mon
            days.add(index)
            if index == end:
                break
            index = (index + 1) % 7
    return frozenset(days)


@dataclass(frozen=True, slots=True)
class Window:
    """One recurring window; ``end`` None means midnight at the end of the day."""

    text: str
    days: frozenset[int]
    start: dtime
    end: dtime | None

    @property
    def crosses_midnight(self) -> bool:
        return self.end is not None and self.end <= self.start

    def covers(self, moment: datetime) -> bool:
        """Whether ``moment`` (wall clock in its own timezone) is inside the window."""

        now = moment.time().replace(tzinfo=None)
        today = moment.weekday()
        if not self.crosses_midnight:
            return today in self.days and self.start <= now and (self.end is None or now < self.end)
        yesterday = (today - 1) % 7
        end = self.end
        return (today in self.days and now >= self.start) or (yesterday in self.days and end is not None and now < end)

    def end_after(self, moment: datetime) -> datetime:
        """When this window (active at ``moment``) closes."""

        day = moment.replace(second=0, microsecond=0)
        if self.end is None:
            return (day + timedelta(days=1)).replace(hour=0, minute=0)
        closing = day.replace(hour=self.end.hour, minute=self.end.minute)
        if self.crosses_midnight and moment.time().replace(tzinfo=None) >= self.start:
            closing += timedelta(days=1)
        return closing


def parse_window(line: str) -> Window:
    """Parse one window line (see the module docstring)."""

    text = " ".join(str(line).strip().lower().split())
    match = WINDOW_RE.match(text)
    if not match:
        raise MaintenanceConfigError(
            f"maintenance window '{line}' must look like 'daily 00:00-06:00', 'sun 01:00-05:00' or "
            "'mon-fri 23:00-02:00'"
        )
    start = _clock(match["start"], allow_end_of_day=False)
    end = _clock(match["end"], allow_end_of_day=True)
    if start is None or start == end:
        raise MaintenanceConfigError(f"maintenance window '{line}' starts and ends at the same time")
    return Window(text=text, days=_days(match["days"]), start=start, end=end)


def parse_windows(raw: object) -> list[Window]:
    """Windows from the option value: a missing option gives the default, an empty list none."""

    if raw is None:
        raw = list(DEFAULT_WINDOWS)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise MaintenanceConfigError("maintenance_windows must be a list such as ['daily 00:00-06:00']")
    return [parse_window(str(item)) for item in raw if str(item).strip()]


def active_window(windows: list[Window], moment: datetime) -> Window | None:
    """The window covering ``moment``, if any."""

    return next((window for window in windows if window.covers(moment)), None)


def window_end(windows: list[Window], moment: datetime) -> datetime | None:
    """When maintenance ends, following back-to-back windows; None when not in one."""

    current = active_window(windows, moment)
    if current is None:
        return None
    end = current.end_after(moment)
    for _ in range(MAX_CHAINED_WINDOWS):
        following = active_window(windows, end)
        if following is None:
            break
        end = following.end_after(end)
    return end


def describe(windows: list[Window]) -> str:
    """'daily 00:00-06:00, sun 01:00-05:00' (or 'none')."""

    return ", ".join(window.text for window in windows) or "none"


def looks_like_maintenance(status: int, body: str = "", title: str = "") -> bool:
    """Whether a vendor response is a maintenance page (HTTP 503 or a short page saying so)."""

    if status in MAINTENANCE_STATUSES:
        return True
    if title and MAINTENANCE_TEXT_RE.search(title):
        return True
    return bool(body) and len(body) <= SHORT_PAGE_CHARS and bool(MAINTENANCE_TEXT_RE.search(body))


@dataclass(slots=True)
class Backoff:
    """Exponential back-off for repeated maintenance answers (with ±10% jitter)."""

    base_seconds: float = 30 * 60
    max_seconds: float = 6 * 3600
    attempts: int = 0
    _random: random.Random = field(default_factory=random.Random, repr=False)

    def next_delay(self) -> float:
        """Seconds to wait after one more maintenance answer."""

        delay = min(self.base_seconds * (2.0**self.attempts), self.max_seconds)
        self.attempts += 1
        return delay * self._random.uniform(0.9, 1.1)

    def reset(self) -> None:
        self.attempts = 0


def resume_jitter_seconds() -> float:
    """A small random delay after a window ends, so not every client calls at once."""

    return random.uniform(60, 600)  # noqa: S311 - jitter only
