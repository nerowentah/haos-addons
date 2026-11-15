"""Tests for the whole-number options: renewal interval (48 h minimum, no maximum) and retries."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import run

BASE: dict[str, object] = {
    "retries": 3,
    "notify_on_failure": True,
    "sessions": [{"name": "TV", "token": "tok"}],
}


def load(**options: object) -> run.Config:
    return run.load_config({**BASE, **options})


class IntervalTest(unittest.TestCase):
    def test_minimum_is_48_and_there_is_no_maximum(self) -> None:
        for saved, expected in ((1, 48), (12, 48), (47, 48), (48, 48), (168, 168), (4000, 4000)):
            with self.subTest(saved=saved):
                self.assertEqual(load(interval_hours=saved).interval_hours, expected)

    def test_missing_or_wrong_type_falls_back_to_the_default(self) -> None:
        self.assertEqual(load().interval_hours, 48)
        for saved in (True, "72", 72.5, None):
            with self.subTest(saved=saved):
                self.assertEqual(load(interval_hours=saved).interval_hours, 48)

    def test_a_raised_value_is_logged_once_per_load(self) -> None:
        with self.assertLogs(run.__name__, level="WARNING") as logs:
            load(interval_hours=24)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("24", logs.output[0])
        with self.assertNoLogs(run.__name__, level="WARNING"):
            load(interval_hours=48)
            load(interval_hours=4000)

    def test_schema_accepts_saved_values_below_the_minimum(self) -> None:
        # A strict int(48,) would make the Supervisor refuse to start installs saved at e.g. 24.
        text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("interval_hours: int(1,)", text)


class RetriesTest(unittest.TestCase):
    def test_retries_stay_between_0_and_10(self) -> None:
        for saved, expected in ((-1, 0), (0, 0), (3, 3), (10, 10), (11, 10)):
            with self.subTest(saved=saved):
                self.assertEqual(load(retries=saved).retries, expected)


if __name__ == "__main__":
    unittest.main()
