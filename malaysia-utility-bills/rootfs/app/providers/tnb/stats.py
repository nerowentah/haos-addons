"""Import myTNB portal data as Home Assistant long-term (external) statistics.

Each sync re-imports the whole fetched window: the cumulative ``sum`` is rebuilt
from the stored sum just before the window, and ``recorder/import_statistics``
upserts rows by their start hour. Hours that TNB publishes late are therefore
corrected on the next sync instead of being dropped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from .mytnb import EnergyPoint

if TYPE_CHECKING:
    import websocket

_LOGGER = logging.getLogger(__name__)

WS_URL = "ws://supervisor/core/websocket"
SOURCE = "power_tnb"
WS_TIMEOUT = 60
BASELINE_LOOKBACK_HOURS = 72
BASELINE_EPOCH = datetime(2015, 1, 1, tzinfo=UTC)


class StatsError(RuntimeError):
    """Raised when the websocket API rejects a request."""


def hourly_buckets(points: Iterable[EnergyPoint]) -> dict[datetime, float]:
    """Sum points into UTC hour buckets."""

    buckets: dict[datetime, float] = {}
    for point in points:
        hour = point.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        buckets[hour] = buckets.get(hour, 0.0) + point.value
    return buckets


def build_rows(buckets: dict[datetime, float], baseline: float) -> list[dict[str, object]]:
    """Cumulative statistics rows starting from ``baseline``."""

    rows: list[dict[str, object]] = []
    total = baseline
    for hour in sorted(buckets):
        total += buckets[hour]
        rows.append({"start": hour.isoformat(), "state": round(buckets[hour], 4), "sum": round(total, 4)})
    return rows


class StatsClient:
    """Minimal Home Assistant websocket client (one connection per sync)."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._ws: websocket.WebSocket | None = None
        self._next_id = 1

    def __enter__(self) -> Self:
        import websocket  # noqa: PLC0415 - loaded only when statistics are imported

        self._ws = websocket.create_connection(WS_URL, timeout=WS_TIMEOUT)
        hello = json.loads(self._ws.recv())
        if hello.get("type") != "auth_required":
            raise StatsError(f"Unexpected websocket greeting: {hello.get('type')}")
        self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(self._ws.recv())
        if reply.get("type") != "auth_ok":
            raise StatsError("Websocket authentication failed")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def call(self, message_type: str, **payload: object) -> object:
        """Send one command and wait for its result."""

        if self._ws is None:
            raise StatsError("Websocket is not connected")
        msg_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": msg_id, "type": message_type, **payload}))
        while True:
            reply = json.loads(self._ws.recv())
            if reply.get("id") != msg_id:
                continue  # unrelated event
            if not reply.get("success"):
                error = reply.get("error") or {}
                raise StatsError(f"{message_type}: {error.get('code')} {error.get('message')}")
            return reply.get("result")

    def _last_sum(self, statistic_id: str, start: datetime, end: datetime, period: str) -> float | None:
        result = self.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=[statistic_id],
            period=period,
            types=["sum"],
        )
        rows = result.get(statistic_id) if isinstance(result, dict) else None
        sums = [row.get("sum") for row in rows or [] if isinstance(row, dict) and row.get("sum") is not None]
        return float(sums[-1]) if sums else None

    def baseline_sum(self, statistic_id: str, before: datetime) -> float:
        """Cumulative sum stored for the hours before ``before`` (0 for a new statistic)."""

        recent = self._last_sum(statistic_id, before - timedelta(hours=BASELINE_LOOKBACK_HOURS), before, "hour")
        if recent is not None:
            return recent
        older = self._last_sum(statistic_id, BASELINE_EPOCH, before, "day")
        return older or 0.0

    def import_statistics(
        self, statistic_id: str, name: str, unit: str, unit_class: str | None, rows: list[dict[str, object]]
    ) -> None:
        """Upsert rows, trying metadata shapes for newer and older HA versions."""

        base = {
            "has_sum": True,
            "name": name,
            "source": SOURCE,
            "statistic_id": statistic_id,
            "unit_of_measurement": unit,
        }
        variants = [
            {**base, "mean_type": 0, "unit_class": unit_class},
            {**base, "mean_type": 0},
            {**base, "has_mean": False},
        ]
        last_error: StatsError | None = None
        for metadata in variants:
            try:
                self.call("recorder/import_statistics", metadata=metadata, stats=rows)
            except StatsError as err:
                last_error = err
                continue
            return
        raise last_error or StatsError("import_statistics failed")


def sync_statistics(  # noqa: PLR0913 - keyword-only payload
    token: str,
    *,
    account_id: str,
    account_name: str,
    window_start: datetime,
    usage: list[EnergyPoint],
    cost: list[EnergyPoint],
) -> None:
    """Import usage (kWh) and cost (MYR) for one account."""

    series = (
        (f"{SOURCE}:usage_{account_id}", f"TNB {account_name} usage", "kWh", "energy", usage),
        (f"{SOURCE}:cost_{account_id}", f"TNB {account_name} cost", "MYR", None, cost),
    )
    with StatsClient(token) as client:
        for statistic_id, name, unit, unit_class, points in series:
            buckets = {hour: value for hour, value in hourly_buckets(points).items() if hour >= window_start}
            if not buckets:
                continue
            first_hour = min(buckets)
            rows = build_rows(buckets, client.baseline_sum(statistic_id, first_hour))
            client.import_statistics(statistic_id, name, unit, unit_class, rows)
            _LOGGER.info("Imported %d hourly %s row(s) for '%s'", len(rows), unit, account_name)
