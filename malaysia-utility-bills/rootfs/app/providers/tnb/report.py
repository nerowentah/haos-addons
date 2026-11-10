"""Daily usage report: one notification per account for each newly complete day."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .mytnb import EnergyPoint

MIN_HOURS_FOR_COMPLETE_DAY = 23  # tolerate one missing hourly reading
SIGNIFICANT_CHANGE_PCT = 1.0


@dataclass(frozen=True, slots=True)
class DailyReport:
    """The newest complete day of portal data for one account."""

    day: date
    usage_kwh: float
    cost_rm: float | None
    previous_kwh: float | None
    cycle_kwh: float
    cycle_cost_rm: float
    cycle_start: date

    @property
    def title(self) -> str:
        return f"TNB usage {self.day:%a %d %b}"

    def message(self, account_name: str) -> str:
        """One short paragraph for a phone notification."""

        cost = f", RM {self.cost_rm:.2f}" if self.cost_rm is not None else ""
        trend = trend_text(self.usage_kwh, self.previous_kwh, long=True)
        trend = f" ({trend})" if trend else ""
        return (
            f"{account_name}: {self.usage_kwh:.2f} kWh{cost}{trend}. "
            f"Cycle since {self.cycle_start:%d %b}: {self.cycle_kwh:.2f} kWh, RM {self.cycle_cost_rm:.2f}."
        )


def trend_text(current: float, previous: float | None, *, long: bool = False) -> str:
    """'▲12%' (or '▲12% vs the day before' when ``long``); '' without a previous day."""

    if not previous:
        return ""
    change = (current - previous) / previous * 100
    if abs(change) < SIGNIFICANT_CHANGE_PCT:
        return "same as the day before" if long else "no change"
    arrow = "▲" if change > 0 else "▼"
    return f"{arrow}{abs(change):.0f}%" + (" vs the day before" if long else "")


def latest_day_caption(history: dict[date, float]) -> str:
    """'6 Nov: 10.89 kWh ▲12%' for the newest day in ``history`` ('' when empty)."""

    if not history:
        return ""
    days = sorted(history)
    day = days[-1]
    trend = trend_text(history[day], history[days[-2]] if len(days) > 1 else None)
    return f"{day.day} {day:%b}: {history[day]:.2f} kWh" + (f" {trend}" if trend else "")


def complete_days(usage: list[EnergyPoint]) -> dict[date, float]:
    """kWh per day, only for days whose hourly readings are (nearly) all in."""

    totals: dict[date, float] = defaultdict(float)
    hours: dict[date, int] = defaultdict(int)
    for point in usage:
        day = point.start.date()
        totals[day] += point.value
        hours[day] += 1
    return {day: totals[day] for day in sorted(totals) if hours[day] >= MIN_HOURS_FOR_COMPLETE_DAY}


def build_report(
    usage: list[EnergyPoint],
    cost: list[EnergyPoint],
    cycle_start_for: Callable[[date], date],
    last_reported: date | None,
) -> DailyReport | None:
    """The report for the newest complete day, or None when it was already reported.

    ``cycle_start_for`` maps a day to the start of its billing cycle: the
    reported day is ~2 days old, so it can belong to the previous cycle.
    """

    days = complete_days(usage)
    if not days:
        return None
    day = max(days)
    if last_reported is not None and day <= last_reported:
        return None
    daily_cost: dict[date, float] = defaultdict(float)
    for point in cost:
        daily_cost[point.start.date()] += point.value
    earlier = [d for d in days if d < day]
    cycle_start = cycle_start_for(day)
    return DailyReport(
        day=day,
        usage_kwh=days[day],
        cost_rm=daily_cost.get(day),
        previous_kwh=days[max(earlier)] if earlier else None,
        cycle_kwh=sum(p.value for p in usage if cycle_start <= p.start.date() <= day),
        cycle_cost_rm=sum(p.value for p in cost if cycle_start <= p.start.date() <= day),
        cycle_start=cycle_start,
    )
