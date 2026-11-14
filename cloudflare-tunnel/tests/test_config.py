"""Tests for option parsing and validation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config


class LoadConfigTest(unittest.TestCase):
    """Option validation."""

    def test_valid(self) -> None:
        cfg = config.load_config(
            {
                "tunnel_token": " tok ",
                "notify_on_failure": False,
                "run_parameters": ["--protocol=quic", " --tag=home "],
                "log_level": "warning",
            }
        )
        self.assertEqual(cfg.tunnel_token.reveal(), "tok")
        self.assertNotIn("tok", repr(cfg.tunnel_token))
        self.assertFalse(cfg.notify_on_failure)
        self.assertEqual(cfg.run_parameters, ("--protocol=quic", "--tag=home"))
        self.assertEqual(cfg.log_level, "warning")
        self.assertEqual(cfg.cloudflared_loglevel, "warn")

    def test_defaults(self) -> None:
        cfg = config.load_config({"tunnel_token": "tok"})
        self.assertTrue(cfg.notify_on_failure)
        self.assertEqual(cfg.run_parameters, ())
        self.assertEqual(cfg.log_level, "info")
        self.assertEqual(cfg.cloudflared_loglevel, "info")

    def test_an_empty_token_means_login_mode(self) -> None:
        self.assertEqual(config.load_config({"tunnel_token": "  "}).mode, config.MODE_LOGIN)
        self.assertEqual(config.load_config({}).mode, config.MODE_LOGIN)
        self.assertEqual(config.load_config({"tunnel_token": "tok"}).mode, config.MODE_TOKEN)

    def test_switching_mode_changes_the_daemon_spec(self) -> None:
        self.assertNotEqual(
            config.load_config({}).daemon_spec(), config.load_config({"tunnel_token": "t"}).daemon_spec()
        )

    def test_hostname_is_normalized(self) -> None:
        cfg = config.load_config({"external_hostname": " Home.Example.COM. "})
        self.assertEqual(cfg.external_hostname, "home.example.com")
        self.assertEqual(config.load_config({"external_hostname": ""}).external_hostname, "")

    def test_hostname_must_be_a_bare_name(self) -> None:
        for bad in (
            "https://home.example.com",
            "home.example.com/x",
            "home.example.com:8123",
            "localhost",
            "-a.example.com",
            "a..b.com",
        ):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load_config({"external_hostname": bad})

    def test_a_new_hostname_does_not_restart_the_connector_by_itself(self) -> None:
        a = config.load_config({"external_hostname": "a.example.com"})
        b = config.load_config({"external_hostname": "b.example.com"})
        self.assertEqual(a.daemon_spec(), b.daemon_spec())  # the app routes it, then restarts

    def test_notify_flag_must_be_bool(self) -> None:
        cfg = config.load_config({"tunnel_token": "tok", "notify_on_failure": "yes"})
        self.assertTrue(cfg.notify_on_failure)

    def test_log_level_unknown_falls_back(self) -> None:
        cfg = config.load_config({"tunnel_token": "tok", "log_level": "LOUD"})
        self.assertEqual(cfg.log_level, "info")

    def test_cloudflared_loglevel_mapping(self) -> None:
        for ours, theirs in (
            ("trace", "debug"),
            ("debug", "debug"),
            ("info", "info"),
            ("notice", "info"),
            ("warning", "warn"),
            ("error", "error"),
            ("fatal", "fatal"),
        ):
            cfg = config.load_config({"tunnel_token": "tok", "log_level": ours})
            self.assertEqual(cfg.cloudflared_loglevel, theirs, ours)

    def test_daemon_spec_changes_with_the_token_loglevel_and_flags(self) -> None:
        base = config.load_config({"tunnel_token": "tok", "run_parameters": ["--region=us"]})
        self.assertEqual(
            base.daemon_spec(),
            config.load_config({"tunnel_token": "tok", "run_parameters": ["--region=us"]}).daemon_spec(),
        )
        self.assertNotEqual(base.daemon_spec(), config.load_config({"tunnel_token": "tok2"}).daemon_spec())
        self.assertNotEqual(
            base.daemon_spec(), config.load_config({"tunnel_token": "tok", "log_level": "debug"}).daemon_spec()
        )
        self.assertNotEqual(base.daemon_spec(), config.load_config({"tunnel_token": "tok"}).daemon_spec())


class RunParametersTest(unittest.TestCase):
    """The run_parameters allowlist."""

    def test_all_allowed_flags(self) -> None:
        for flag in config.ALLOWED_RUN_PARAMETERS:
            cfg = config.load_config({"tunnel_token": "tok", "run_parameters": [f"{flag}=x1"]})
            self.assertEqual(cfg.run_parameters, (f"{flag}=x1",), flag)

    def test_empty_and_blank_entries_are_skipped(self) -> None:
        cfg = config.load_config({"tunnel_token": "tok", "run_parameters": ["", "   ", "--region=us"]})
        self.assertEqual(cfg.run_parameters, ("--region=us",))

    def test_missing_key_is_empty(self) -> None:
        self.assertEqual(config.load_config({"tunnel_token": "tok"}).run_parameters, ())

    def test_not_a_list(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config({"tunnel_token": "tok", "run_parameters": "--region=us"})

    def test_unknown_flag_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config({"tunnel_token": "tok", "run_parameters": ["--logfile=/tmp/log"]})
        self.assertIn("not allowed", str(ctx.exception))

    def test_missing_dashes_are_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config({"tunnel_token": "tok", "run_parameters": ["region=us"]})

    def test_bare_flag_without_value_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config({"tunnel_token": "tok", "run_parameters": ["--region"]})


class LoadOptionsTest(unittest.TestCase):
    """Reading /data/options.json."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "options.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_a_mapping(self) -> None:
        self.path.write_text('{"tunnel_token": "tok"}', encoding="utf-8")
        self.assertEqual(config.load_options(self.path), {"tunnel_token": "tok"})

    def test_missing_file(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_options(self.path)

    def test_broken_json(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_options(self.path)

    def test_non_mapping_root(self) -> None:
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_options(self.path)


if __name__ == "__main__":
    unittest.main()
