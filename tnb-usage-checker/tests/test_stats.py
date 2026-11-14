"""Tests for the long-term statistics import (no websocket, no network)."""

from __future__ import annotations

import json
import sys
import types
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import stats
from mytnb import EnergyPoint

MYT = stats.MYT
USAGE_ID = "power_tnb:usage_home"
COST_ID = "power_tnb:cost_home"
WINDOW = datetime(2026, 10, 1, 0, 0, tzinfo=MYT)


def point(hours: float, value: float, start: datetime = WINDOW) -> EnergyPoint:
    return EnergyPoint(start + timedelta(hours=hours), value)


Handler = Callable[[str, dict[str, Any]], object]


class RecordingClient(stats.StatsClient):
    """StatsClient whose websocket calls are answered by a handler."""

    def __init__(self, handler: Handler | None = None) -> None:
        super().__init__("token")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handler: Handler = handler or (lambda _type, _payload: {})

    def call(self, message_type: str, **payload: object) -> object:
        self.calls.append((message_type, payload))
        return self.handler(message_type, payload)


class FakeSocket:
    """Minimal stand-in for ``websocket.WebSocket``."""

    def __init__(self, incoming: list[object]) -> None:
        self.incoming = [json.dumps(message) for message in incoming]
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def recv(self) -> str:
        return self.incoming.pop(0)

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def close(self) -> None:
        self.closed = True


def connect(socket: FakeSocket) -> AbstractContextManager[object]:
    """Make ``import websocket`` inside ``StatsClient.__enter__`` return ``socket``."""

    module = types.ModuleType("websocket")
    vars(module)["create_connection"] = lambda *_args, **_kwargs: socket
    return mock.patch.dict(sys.modules, {"websocket": module})


def connected(incoming: list[object]) -> tuple[stats.StatsClient, FakeSocket]:
    """A client that is already talking to a scripted socket."""

    client, socket = stats.StatsClient("token"), FakeSocket(incoming)
    mock.patch.object(client, "_ws", socket).start()  # stands in for websocket.WebSocket
    return client, socket


class BucketTest(unittest.TestCase):
    def test_sums_points_in_the_same_utc_hour(self) -> None:
        buckets = stats.hourly_buckets([point(1, 1.5), point(1.5, 0.5), point(2, 3.0)])
        self.assertEqual(
            buckets,
            {
                datetime(2026, 9, 30, 17, 0, tzinfo=UTC): 2.0,
                datetime(2026, 9, 30, 18, 0, tzinfo=UTC): 3.0,
            },
        )

    def test_empty(self) -> None:
        self.assertEqual(stats.hourly_buckets([]), {})

    def test_build_rows_is_cumulative(self) -> None:
        buckets = {datetime(2026, 10, 1, 1, 0, tzinfo=UTC): 1.5, datetime(2026, 10, 1, 2, 0, tzinfo=UTC): 2.0}
        rows = stats.build_rows(buckets, baseline=10.0)
        self.assertEqual([row["sum"] for row in rows], [11.5, 13.5])
        self.assertEqual([row["state"] for row in rows], [1.5, 2.0])
        self.assertEqual(rows[0]["start"], "2026-10-01T01:00:00+00:00")

    def test_build_rows_empty(self) -> None:
        self.assertEqual(stats.build_rows({}, baseline=10.0), [])

    def test_statistic_ids(self) -> None:
        self.assertEqual(stats.statistic_ids("home"), (USAGE_ID, COST_ID))


class SyncStatisticsTest(unittest.TestCase):
    class FakeClient:
        instances: ClassVar[list[SyncStatisticsTest.FakeClient]] = []
        next_error: ClassVar[Exception | None] = None

        def __init__(self, token: str) -> None:
            self.token = token
            self.baselines: list[tuple[str, datetime]] = []
            self.imports: list[tuple[str, str, str, str | None, list[dict[str, object]]]] = []
            self.baseline_value = 10.0
            self.error = type(self).next_error
            type(self).instances.append(self)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def baseline_sum(self, statistic_id: str, before: datetime) -> float:
            self.baselines.append((statistic_id, before))
            return self.baseline_value

        def import_statistics(
            self,
            statistic_id: str,
            name: str,
            unit: str,
            unit_class: str | None,
            rows: list[dict[str, object]],
        ) -> None:
            if self.error is not None:
                raise self.error
            self.imports.append((statistic_id, name, unit, unit_class, rows))

    def setUp(self) -> None:
        self.FakeClient.instances.clear()
        self.FakeClient.next_error = None
        patcher = mock.patch.object(stats, "StatsClient", self.FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, usage: list[EnergyPoint], cost: list[EnergyPoint]) -> stats.ImportResult:
        return stats.sync_statistics(
            "token",
            account_id="home",
            account_name="Home",
            window_start=WINDOW,
            usage=usage,
            cost=cost,
        )

    def client(self) -> SyncStatisticsTest.FakeClient:
        return self.FakeClient.instances[0]

    def test_imports_usage_then_cost_with_the_right_metadata(self) -> None:
        result = self.sync([point(1, 1.5), point(1.5, 0.5), point(2, 3.0)], [point(5, 4.2)])
        client = self.client()

        self.assertEqual(client.token, "token")
        self.assertEqual([call[0] for call in client.imports], [USAGE_ID, COST_ID])
        self.assertEqual([call[1] for call in client.imports], ["TNB Home usage", "TNB Home cost"])
        self.assertEqual([call[2] for call in client.imports], ["kWh", "MYR"])
        self.assertEqual([call[3] for call in client.imports], ["energy", None])
        self.assertEqual([row["sum"] for row in client.imports[0][4]], [12.0, 15.0])
        self.assertEqual([row["sum"] for row in client.imports[1][4]], [14.2])
        self.assertEqual(result.usage_rows, 2)
        self.assertEqual(result.cost_rows, 1)
        self.assertEqual(result.first_hour, datetime(2026, 9, 30, 17, 0, tzinfo=UTC))
        self.assertEqual(result.last_hour, datetime(2026, 9, 30, 21, 0, tzinfo=UTC))

    def test_points_before_the_window_are_dropped(self) -> None:
        result = self.sync([point(-1, 99.0), point(1, 2.0)], [])
        client = self.client()

        self.assertEqual(client.baselines, [(USAGE_ID, datetime(2026, 9, 30, 17, 0, tzinfo=UTC))])
        self.assertEqual([row["state"] for row in client.imports[0][4]], [2.0])
        self.assertEqual(result.usage_rows, 1)
        self.assertEqual(result.cost_rows, 0)

    def test_an_empty_series_is_skipped(self) -> None:
        result = self.sync([], [point(5, 4.2)])
        client = self.client()

        self.assertEqual([call[0] for call in client.baselines], [COST_ID])
        self.assertEqual([call[0] for call in client.imports], [COST_ID])
        self.assertEqual(result.usage_rows, 0)
        self.assertEqual(result.cost_rows, 1)
        self.assertEqual(result.first_hour, datetime(2026, 9, 30, 21, 0, tzinfo=UTC))

    def test_nothing_to_import(self) -> None:
        result = self.sync([], [])
        self.assertEqual(result, stats.ImportResult())
        self.assertEqual(self.client().imports, [])

    def test_errors_propagate(self) -> None:
        self.FakeClient.next_error = stats.StatsError("import_statistics: unknown_error boom")
        with self.assertRaises(stats.StatsError):
            self.sync([point(1, 2.0)], [])


class BaselineTest(unittest.TestCase):
    def client(self, handler: Handler) -> RecordingClient:
        return RecordingClient(handler)

    def test_a_new_statistic_starts_at_zero(self) -> None:
        client = self.client(lambda _type, _payload: [])
        self.assertEqual(client.baseline_sum(USAGE_ID, WINDOW), 0.0)
        self.assertEqual([call[0] for call in client.calls], ["recorder/get_statistics_metadata"])

    def test_a_different_statistic_id_is_not_a_match(self) -> None:
        client = self.client(lambda _type, _payload: [{"statistic_id": COST_ID}])
        self.assertEqual(client.baseline_sum(USAGE_ID, WINDOW), 0.0)

    def test_uses_the_last_hourly_sum(self) -> None:
        def handler(message_type: str, _payload: dict[str, object]) -> object:
            if message_type == "recorder/get_statistics_metadata":
                return [{"statistic_id": USAGE_ID}]
            return {USAGE_ID: [{"sum": 12.5}]}

        client = self.client(handler)
        self.assertEqual(client.baseline_sum(USAGE_ID, WINDOW), 12.5)
        self.assertEqual(
            [call[0] for call in client.calls],
            ["recorder/get_statistics_metadata", "recorder/statistics_during_period"],
        )
        payload = client.calls[1][1]
        self.assertEqual(payload["period"], "hour")
        self.assertEqual(payload["start_time"], (WINDOW - timedelta(hours=72)).isoformat())
        self.assertEqual(payload["end_time"], WINDOW.isoformat())

    def test_daily_fallback_stops_before_the_first_day(self) -> None:
        def handler(message_type: str, payload: dict[str, object]) -> object:
            if message_type == "recorder/get_statistics_metadata":
                return [{"statistic_id": USAGE_ID}]
            return {} if payload["period"] == "hour" else {USAGE_ID: [{"sum": 40.0}]}

        client = self.client(handler)
        self.assertEqual(client.baseline_sum(USAGE_ID, WINDOW), 40.0)
        payload = client.calls[2][1]
        self.assertEqual(payload["period"], "day")
        # 00:00 MYT on 1 Oct minus one second: Home Assistant rounds a daily end
        # up to the next local midnight, so the first day of the window is excluded.
        self.assertEqual(payload["end_time"], "2026-09-30T23:59:59+08:00")

    def test_malformed_replies_raise(self) -> None:
        def handler(message_type: str, _payload: dict[str, object]) -> object:
            if message_type == "recorder/get_statistics_metadata":
                return [{"statistic_id": USAGE_ID}]
            return []  # a list instead of the expected {statistic_id: rows} mapping

        client = self.client(handler)
        with self.assertRaises(stats.StatsError):
            client.baseline_sum(USAGE_ID, WINDOW)

    def test_a_non_numeric_sum_raises(self) -> None:
        def handler(message_type: str, _payload: dict[str, object]) -> object:
            if message_type == "recorder/get_statistics_metadata":
                return [{"statistic_id": USAGE_ID}]
            return {USAGE_ID: [{"sum": "lots"}]}

        with self.assertRaises(stats.StatsError):
            self.client(handler).baseline_sum(USAGE_ID, WINDOW)


class ImportStatisticsTest(unittest.TestCase):
    def client(self, failures: int) -> RecordingClient:
        remaining = {"count": failures}

        def handler(message_type: str, _payload: dict[str, object]) -> object:
            if message_type != "recorder/import_statistics":
                return {}
            if remaining["count"] > 0:
                remaining["count"] -= 1
                raise stats.StatsError("import_statistics: unknown_error rejected")
            return None

        return RecordingClient(handler)

    def test_modern_metadata_is_tried_first(self) -> None:
        client = self.client(0)
        client.import_statistics(USAGE_ID, "TNB Home usage", "kWh", "energy", [{"start": "x"}])

        self.assertEqual(len(client.calls), 1)
        metadata = client.calls[0][1]["metadata"]
        self.assertEqual(metadata["mean_type"], 0)
        self.assertEqual(metadata["unit_class"], "energy")
        self.assertNotIn("has_mean", metadata)
        self.assertEqual(metadata["source"], "power_tnb")

    def test_legacy_shapes_are_only_fallbacks(self) -> None:
        client = self.client(2)
        client.import_statistics(USAGE_ID, "TNB Home usage", "kWh", "energy", [{"start": "x"}])

        self.assertEqual(len(client.calls), 3)
        first = client.calls[0][1]["metadata"]
        second = client.calls[1][1]["metadata"]
        third = client.calls[2][1]["metadata"]
        self.assertEqual(first["unit_class"], "energy")
        self.assertNotIn("unit_class", second)
        self.assertNotIn("mean_type", third)
        self.assertIs(third["has_mean"], False)

    def test_all_shapes_failing_raises_the_last_error(self) -> None:
        client = self.client(3)
        with self.assertRaises(stats.StatsError) as caught:
            client.import_statistics(USAGE_ID, "TNB Home usage", "kWh", "energy", [{"start": "x"}])
        self.assertIn("rejected", str(caught.exception))


class WebsocketTest(unittest.TestCase):
    def test_authenticates(self) -> None:
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_ok"}])
        with connect(socket):
            client = stats.StatsClient("token")
            client.__enter__()
        self.assertEqual(socket.sent[0], {"type": "auth", "access_token": "token"})
        client.__exit__()
        self.assertTrue(socket.closed)

    def test_rejects_an_unexpected_greeting(self) -> None:
        socket = FakeSocket([{"type": "auth_ok"}])
        with connect(socket), self.assertRaises(stats.StatsError):
            stats.StatsClient("token").__enter__()

    def test_rejects_a_failed_login(self) -> None:
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_invalid"}])
        with connect(socket), self.assertRaises(stats.StatsError):
            stats.StatsClient("token").__enter__()

    def test_call_ignores_unrelated_messages(self) -> None:
        client, socket = connected(
            [
                {"id": 99, "type": "event", "event": {}},
                {"id": 1, "type": "result", "success": True, "result": {"ok": True}},
            ]
        )
        self.assertEqual(client.call("recorder/statistics_during_period", statistic_ids=[USAGE_ID]), {"ok": True})
        self.assertEqual(socket.sent[0]["id"], 1)
        self.assertEqual(socket.sent[0]["statistic_ids"], [USAGE_ID])

    def test_call_reports_errors(self) -> None:
        client, _socket = connected(
            [{"id": 1, "type": "result", "success": False, "error": {"code": "unknown_error", "message": "nope"}}]
        )
        with self.assertRaises(stats.StatsError) as caught:
            client.call("recorder/import_statistics")
        self.assertIn("unknown_error", str(caught.exception))
        self.assertIn("nope", str(caught.exception))

    def test_call_requires_a_connection(self) -> None:
        with self.assertRaises(stats.StatsError):
            stats.StatsClient("token").call("recorder/import_statistics")


if __name__ == "__main__":
    unittest.main()
