"""Tests for the options: nothing deletes on its own unless the options say so."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config


class ConfigTest(unittest.TestCase):
    def test_everything_automatic_is_off_by_default(self) -> None:
        loaded = config.load_config({})
        self.assertFalse(loaded.auto_clean)
        self.assertFalse(loaded.auto_clean_backups)
        self.assertFalse(loaded.backups_auto_deleted)
        self.assertEqual(loaded.keep_backups, 3)
        self.assertEqual(loaded.low_space_percent, 10)
        self.assertTrue(loaded.notify_on_low_space)
        self.assertEqual(loaded.log_level, "info")

    def test_backups_need_both_switches(self) -> None:
        self.assertFalse(config.load_config({"auto_clean_backups": True}).backups_auto_deleted)
        self.assertFalse(config.load_config({"auto_clean": True}).backups_auto_deleted)
        self.assertTrue(config.load_config({"auto_clean": True, "auto_clean_backups": True}).backups_auto_deleted)

    def test_a_switch_that_is_not_true_or_false_is_refused(self) -> None:
        for name in ("auto_clean", "auto_clean_backups", "notify_on_low_space"):
            for value in ("true", 1, None, "yes"):
                with self.subTest(name=name, value=value), self.assertRaises(config.ConfigError):
                    config.load_config({name: value})

    def test_numbers_out_of_range_are_refused(self) -> None:
        for name, values in (("keep_backups", (0, 51, True, "3", 2.5)), ("low_space_percent", (0, 51, False, "10"))):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(config.ConfigError):
                    config.load_config({name: value})
        self.assertEqual(config.load_config({"keep_backups": 1}).keep_backups, 1)
        self.assertEqual(config.load_config({"low_space_percent": 50}).low_space_percent, 50)

    def test_log_level_falls_back_to_info(self) -> None:
        self.assertEqual(config.parse_log_level({"log_level": "DEBUG"}), "debug")
        self.assertEqual(config.parse_log_level({"log_level": "loud"}), "info")

    def test_options_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options.json"
            with self.assertRaises(config.ConfigError):
                config.load_options(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load_options(path)
            path.write_text(json.dumps({"auto_clean": True}), encoding="utf-8")
            self.assertTrue(config.load_config(config.load_options(path)).auto_clean)


if __name__ == "__main__":
    unittest.main()
