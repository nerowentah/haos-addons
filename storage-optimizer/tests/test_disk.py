"""Tests for reading the Supervisor's disk usage report and the free-space history."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import disk
from helpers import GB, usage_reply


class UsageTest(unittest.TestCase):
    def test_the_eight_areas_largest_first(self) -> None:
        usage = disk.parse_usage(usage_reply())
        self.assertEqual(usage.total, 32 * GB)
        self.assertEqual(usage.free, 12 * GB)
        self.assertEqual(len(usage.buckets), 8)
        self.assertEqual([b.id for b in usage.buckets[:3]], ["system", "addons_data", "backup"])
        self.assertEqual(usage.buckets[1].label, "App data")

    def test_children_are_sorted_and_keep_their_names(self) -> None:
        usage = disk.parse_usage(usage_reply())
        apps = usage.bucket("addons_data", "apps_data")
        assert apps is not None
        self.assertEqual([(c.id, c.used) for c in apps.children], [("a1b2c3d4_example", 4 * GB), ("core_example", GB)])
        home = usage.bucket("homeassistant")
        assert home is not None
        self.assertEqual(home.children[0].label, "home-assistant_v2.db")

    def test_an_unknown_area_is_kept(self) -> None:
        reply = usage_reply()
        children = reply["children"]
        assert isinstance(children, list)
        children.append({"id": "something_new", "label": "Something new", "used_bytes": 7})
        children.append({"label": "No id", "used_bytes": 3})
        children.append("not an area")
        usage = disk.parse_usage(reply)
        self.assertEqual(len(usage.buckets), 10)
        new = usage.bucket("something_new")
        assert new is not None
        self.assertEqual(new.label, "Something new")
        self.assertIsNotNone(usage.bucket("No id"))

    def test_the_newer_api_names_get_the_same_labels(self) -> None:
        usage = disk.parse_usage(
            {"total_bytes": 10, "used_bytes": 5, "children": [{"id": "apps_data", "label": "x", "used_bytes": 1}]}
        )
        self.assertEqual(usage.buckets[0].label, "App data")

    def test_a_reply_without_a_size_is_refused(self) -> None:
        replies: tuple[object, ...] = (None, [], {}, {"total_bytes": 0}, {"total_bytes": "32"}, {"total_bytes": True})
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(disk.DiskError):
                disk.parse_usage(reply)

    def test_bad_numbers_count_as_zero_and_used_never_exceeds_total(self) -> None:
        usage = disk.parse_usage(
            {"total_bytes": 100, "used_bytes": 500, "children": [{"id": "media", "used_bytes": -5}]}
        )
        self.assertEqual(usage.used, 100)
        self.assertEqual(usage.free, 0)
        self.assertEqual(usage.buckets[0].used, 0)

    def test_low_space(self) -> None:
        self.assertFalse(disk.is_low(disk.parse_usage(usage_reply(used=20 * GB)), 10))
        self.assertTrue(disk.is_low(disk.parse_usage(usage_reply(used=30 * GB)), 10))


class FormatTest(unittest.TestCase):
    def test_human_sizes(self) -> None:
        cases = {
            0: "0 B",
            999: "999 B",
            1000: "1.0 KB",
            1_500_000: "1.5 MB",
            2 * GB: "2.0 GB",
            256 * GB: "256 GB",
            5000 * GB: "5.0 TB",
            5_000_000 * GB: "5000 TB",
            -4: "0 B",
        }
        for size, text in cases.items():
            with self.subTest(size=size):
                self.assertEqual(disk.human(size), text)

    def test_percent(self) -> None:
        self.assertEqual(disk.percent(0, 100), "0%")
        self.assertEqual(disk.percent(1, 1000), "<1%")
        self.assertEqual(disk.percent(25, 100), "25%")
        self.assertEqual(disk.percent(5, 0), "0%")


class HistoryTest(unittest.TestCase):
    def test_one_sample_per_day_and_thirty_days_at_most(self) -> None:
        history: dict[str, object] = {}
        usage = disk.parse_usage(usage_reply())
        self.assertTrue(disk.add_sample(history, "2026-01-01", usage))
        self.assertFalse(disk.add_sample(history, "2026-01-01", usage))
        self.assertTrue(disk.add_sample(history, "2026-01-01", disk.parse_usage(usage_reply(used=21 * GB))))
        for day in range(2, 32):
            disk.add_sample(history, f"2026-01-{day:02d}", usage)
        disk.add_sample(history, "2026-02-01", usage)
        self.assertEqual(len(history), 30)
        self.assertNotIn("2026-01-01", history)
        self.assertNotIn("2026-01-02", history)

    def test_chart_bars_skip_rows_that_are_not_samples(self) -> None:
        history: dict[str, object] = {
            "2026-01-01": {"free": 12 * GB, "total": 32 * GB},
            "2026-01-02": "damaged",
            "2026-01-03": {"free": 11.5 * GB, "total": 32 * GB},
        }
        self.assertEqual(disk.chart_bars(history), [("01-01", 12.0), ("01-03", 11.5)])


if __name__ == "__main__":
    unittest.main()
