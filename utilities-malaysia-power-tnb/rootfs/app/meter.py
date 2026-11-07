"""Accumulates kWh from Home Assistant energy sensors into TNB billing buckets."""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import tariff

_LOGGER = logging.getLogger(__name__)

MYT = timezone(timedelta(hours=8), "MYT")  # Malaysia has no DST
PEAK_START = time(14, 0)
PEAK_END = time(22, 0)
WEEKDAYS = 5  # Monday..Friday are weekdays (0..4)

MAX_KWH_PER_5_MIN = 10.0  # larger jumps are treated as spikes
SPIKE_MIN_ELAPSED_MINUTES = 5.0
MAX_CONSECUTIVE_REJECTIONS = 3
HISTORY_MONTHS = 12
HISTORY_FOR_PREDICTION = 3
ENERGY_UNITS = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}

HolidayCheck = Callable[[date], bool]


def to_kwh(value: float, unit: str | None) -> float:
    """Convert an energy reading to kWh (unknown units are assumed to be kWh)."""

    return value * ENERGY_UNITS.get((unit or "kwh").strip().lower(), 1.0)


def is_peak(moment: datetime, is_holiday: HolidayCheck) -> bool:
    """Return whether a moment is in the ToU peak window (weekdays 14:00-22:00, not holidays)."""

    local = moment.astimezone(MYT)
    if local.weekday() >= WEEKDAYS or is_holiday(local.date()):
        return False
    return PEAK_START <= local.time() < PEAK_END


def peak_fraction(start: datetime, end: datetime, is_holiday: HolidayCheck) -> float:
    """Return the share of ``[start, end)`` that falls inside the peak window."""

    start = start.astimezone(MYT)
    end = end.astimezone(MYT)
    total = (end - start).total_seconds()
    if total <= 0:
        return 1.0 if is_peak(end, is_holiday) else 0.0

    peak_seconds = 0.0
    cursor = start
    while cursor < end:
        day_start = datetime.combine(cursor.date(), time(0), MYT)
        boundaries = [
            datetime.combine(cursor.date(), PEAK_START, MYT),
            datetime.combine(cursor.date(), PEAK_END, MYT),
            day_start + timedelta(days=1),
        ]
        segment_end = min([b for b in boundaries if b > cursor] + [end])
        if is_peak(cursor, is_holiday):
            peak_seconds += (segment_end - cursor).total_seconds()
        cursor = segment_end
    return peak_seconds / total


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def billing_period_start(day: date, start_day: int) -> date:
    """Return the first day of the billing cycle that contains ``day``."""

    this_month = _clamped(day.year, day.month, start_day)
    if day >= this_month:
        return this_month
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return _clamped(year, month, start_day)


def next_period_start(period_start: date, start_day: int) -> date:
    """Return the first day of the billing cycle after the one starting at ``period_start``."""

    year, month = (period_start.year, period_start.month + 1)
    if month > 12:  # noqa: PLR2004 - months in a year
        year, month = year + 1, 1
    return _clamped(year, month, start_day)


@dataclass(slots=True)
class Counter:
    """Turns a cumulative energy reading into deltas, guarding against resets and spikes."""

    last_value: float | None = None
    last_ts: float | None = None
    rejections: int = 0

    def update(self, value: float, ts: float) -> float:
        """Return the kWh consumed since the last accepted reading (0 when nothing counts)."""

        if self.last_value is None or self.last_ts is None:
            self.last_value, self.last_ts = value, ts
            return 0.0

        delta = value - self.last_value
        if delta < 0:
            _LOGGER.info("Energy meter went backwards (reset or replacement); re-baselining")
            self.last_value, self.last_ts, self.rejections = value, ts, 0
            return 0.0

        elapsed_min = max((ts - self.last_ts) / 60, SPIKE_MIN_ELAPSED_MINUTES)
        allowed = elapsed_min / SPIKE_MIN_ELAPSED_MINUTES * MAX_KWH_PER_5_MIN
        if delta > allowed:
            self.rejections += 1
            if self.rejections >= MAX_CONSECUTIVE_REJECTIONS:
                _LOGGER.warning("Accepting a new energy baseline after %d spike rejections", self.rejections)
                self.last_value, self.last_ts, self.rejections = value, ts, 0
            else:
                _LOGGER.warning("Ignoring an energy spike of %.2f kWh (allowed %.2f kWh)", delta, allowed)
            return 0.0

        self.last_value, self.last_ts, self.rejections = value, ts, 0
        return delta


@dataclass(slots=True)
class AccountMeter:
    """Billing-cycle and daily buckets for one TNB account, persisted in the store."""

    billing_start_day: int = 1
    import_counter: Counter = field(default_factory=Counter)
    export_counter: Counter = field(default_factory=Counter)
    period_start: str = ""
    peak_kwh: float = 0.0
    offpeak_kwh: float = 0.0
    export_kwh: float = 0.0
    day: str = ""
    today_peak_kwh: float = 0.0
    today_offpeak_kwh: float = 0.0
    today_export_kwh: float = 0.0
    nem_carry_kwh: float = 0.0
    history: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, object] | None, billing_start_day: int) -> AccountMeter:
        """Restore a meter from the store (tolerates missing or malformed fields)."""

        meter = cls(billing_start_day=billing_start_day)
        if not isinstance(data, dict):
            return meter
        for name in ("import_counter", "export_counter"):
            raw = data.get(name)
            if isinstance(raw, dict):
                setattr(
                    meter,
                    name,
                    Counter(
                        last_value=_opt_float(raw.get("last_value")),
                        last_ts=_opt_float(raw.get("last_ts")),
                        rejections=int(raw.get("rejections") or 0),
                    ),
                )
        for name in ("period_start", "day"):
            setattr(meter, name, str(data.get(name) or ""))
        for name in (
            "peak_kwh",
            "offpeak_kwh",
            "export_kwh",
            "today_peak_kwh",
            "today_offpeak_kwh",
            "today_export_kwh",
            "nem_carry_kwh",
        ):
            setattr(meter, name, _opt_float(data.get(name)) or 0.0)
        history = data.get("history")
        if isinstance(history, list):
            meter.history = [h for h in history if isinstance(h, dict)][-HISTORY_MONTHS:]
        return meter

    def to_dict(self) -> dict[str, object]:
        """Serialise for the store."""

        data = asdict(self)
        data.pop("billing_start_day")
        return data

    def roll_over(self, now: datetime, tariff_name: str, afa_rate: float) -> None:
        """Archive the finished billing cycle and reset daily buckets when the date changes."""

        local_day = now.astimezone(MYT).date()
        current_period = billing_period_start(local_day, self.billing_start_day).isoformat()
        if self.period_start and self.period_start != current_period:
            bill = self.bill(tariff_name, afa_rate)
            self.history.append(
                {
                    "period_start": self.period_start,
                    "import_kwh": round(bill.import_kwh, 3),
                    "peak_kwh": round(self.peak_kwh, 3),
                    "offpeak_kwh": round(self.offpeak_kwh, 3),
                    "export_kwh": round(self.export_kwh, 3),
                    "total": bill.total,
                }
            )
            self.history = self.history[-HISTORY_MONTHS:]
            self.nem_carry_kwh = bill.nem_excess_kwh
            self.peak_kwh = self.offpeak_kwh = self.export_kwh = 0.0
            _LOGGER.info("Billing cycle %s closed: %.2f kWh, RM %.2f", self.period_start, bill.import_kwh, bill.total)
        self.period_start = current_period

        if self.day != local_day.isoformat():
            self.today_peak_kwh = self.today_offpeak_kwh = self.today_export_kwh = 0.0
            self.day = local_day.isoformat()

    def ingest(
        self,
        now: datetime,
        import_kwh: float | None,
        export_kwh: float | None,
        is_holiday: HolidayCheck,
    ) -> None:
        """Add new readings (cumulative kWh) to the buckets. Call ``roll_over`` first."""

        ts = now.timestamp()
        if import_kwh is not None:
            previous_ts = self.import_counter.last_ts
            delta = self.import_counter.update(import_kwh, ts)
            if delta > 0:
                start = datetime.fromtimestamp(previous_ts if previous_ts is not None else ts, MYT)
                share = peak_fraction(start, now, is_holiday)
                self.peak_kwh += delta * share
                self.offpeak_kwh += delta * (1 - share)
                self.today_peak_kwh += delta * share
                self.today_offpeak_kwh += delta * (1 - share)
        if export_kwh is not None:
            delta = self.export_counter.update(export_kwh, ts)
            if delta > 0:
                self.export_kwh += delta
                self.today_export_kwh += delta

    def bill(self, tariff_name: str, afa_rate: float) -> tariff.Bill:
        """Bill estimate for the cycle so far."""

        return tariff.calculate(
            self.peak_kwh,
            self.offpeak_kwh,
            self.export_kwh,
            tariff=tariff_name,
            afa_rate=afa_rate,
            nem_carry_kwh=self.nem_carry_kwh,
        )

    def predicted_bill(self, now: datetime, tariff_name: str, afa_rate: float) -> tariff.Bill | None:
        """Projected end-of-cycle bill from the current trend blended with recent history."""

        start = date.fromisoformat(self.period_start) if self.period_start else None
        if start is None:
            return None
        start_dt = datetime.combine(start, time(0), MYT)
        cycle_days = (next_period_start(start, self.billing_start_day) - start).days
        elapsed_days = (now.astimezone(MYT) - start_dt).total_seconds() / 86400
        used = self.peak_kwh + self.offpeak_kwh

        recent = [float(h.get("import_kwh") or 0.0) for h in self.history[-HISTORY_FOR_PREDICTION:]]
        history_kwh = sum(recent) / len(recent) if recent else None
        trend_kwh = used / elapsed_days * cycle_days if elapsed_days >= 0.5 else None  # noqa: PLR2004 - half a day

        if trend_kwh is None and history_kwh is None:
            return None
        if trend_kwh is None or history_kwh is None:
            predicted = trend_kwh if trend_kwh is not None else history_kwh
        else:
            weight = 0.3 if elapsed_days < 7 else 0.6 if elapsed_days < 21 else 0.8  # noqa: PLR2004 - blend schedule
            predicted = trend_kwh * weight + history_kwh * (1 - weight)
        predicted = max(predicted or 0.0, used)

        peak_share = self.peak_kwh / used if used > 0 else 0.5
        export_ratio = self.export_kwh / used if used > 0 else 0.0
        return tariff.calculate(
            predicted * peak_share,
            predicted * (1 - peak_share),
            predicted * export_ratio,
            tariff=tariff_name,
            afa_rate=afa_rate,
            nem_carry_kwh=self.nem_carry_kwh,
        )

    def days_remaining(self, now: datetime) -> int:
        """Whole days until the next billing cycle starts."""

        if not self.period_start:
            return 0
        nxt = next_period_start(date.fromisoformat(self.period_start), self.billing_start_day)
        return max((nxt - now.astimezone(MYT).date()).days, 0)


def _opt_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
