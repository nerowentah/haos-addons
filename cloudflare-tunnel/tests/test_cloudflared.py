"""Tests for the cloudflared process supervisor: token safety, states, restarts (no network)."""

from __future__ import annotations

import io
import sys
import time
import unittest
import unittest.mock
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import cloudflared as cf
import config

TOKEN = "secret-token-value"  # noqa: S105 - a fake token for the tests


class FakeProcess:
    """A Popen stand-in with pre-filled output."""

    def __init__(self, output: bytes = b"", returncode: int = 0) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode: int | None = None
        self._code = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._code

    def kill(self) -> None:
        self.killed = True
        self.returncode = 9

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the real Popen takes it
        return self.returncode if self.returncode is not None else 0


class FakeSpawn:
    """Records every start and hands out the prepared processes one by one."""

    def __init__(self, *processes: FakeProcess) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.pending = list(processes)
        self.last: FakeProcess | None = None

    def __call__(self, argv: list[str], env: dict[str, str]) -> FakeProcess:
        self.argvs.append(list(argv))
        self.envs.append(dict(env))
        process = self.pending.pop(0) if self.pending else FakeProcess()
        self.last = process
        return process


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def no_metrics() -> str | None:
    """The connector's metrics page is unreachable (the default in these tests)."""

    return None


def daemon(
    spawn: FakeSpawn, clock: FakeClock, metrics: cf.MetricsFn = no_metrics, **overrides: object
) -> cf.Cloudflared:
    raw = {"tunnel_token": TOKEN, "run_parameters": ["--region=us"], **overrides}
    return cf.Cloudflared(config.load_config(raw), spawn=spawn, clock=clock, metrics=metrics)


def settled(daemon_obj: cf.Cloudflared, seconds: float = 2.0, until: Callable[[], bool] | None = None) -> None:
    """Give the reader thread time to drain the fake output."""

    def done() -> bool:
        if until is not None:
            return until()
        return daemon_obj.status.state in (cf.STATE_CONNECTED, cf.STATE_ERROR, cf.STATE_STOPPED)

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        daemon_obj.poll()
        if done():
            return
        time.sleep(0.01)
    daemon_obj.poll()


class ArgvTest(unittest.TestCase):
    """What cloudflared is started with."""

    def test_token_never_on_the_command_line(self) -> None:
        spawn = FakeSpawn()
        d = daemon(spawn, FakeClock())
        d.start()
        argv = spawn.argvs[-1]
        self.assertEqual(argv[0], cf.CLOUDFLARED)
        self.assertEqual(argv[1], "tunnel")
        self.assertEqual(argv[-1], "run")
        self.assertIn("--no-autoupdate", argv)
        self.assertIn("--metrics=127.0.0.1:36500", argv)
        self.assertIn("--loglevel=info", argv)
        self.assertIn("--region=us", argv)
        self.assertNotIn("run", argv[2:-1])  # flags live between 'tunnel' and 'run'
        self.assertNotIn(TOKEN, argv)
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(spawn.envs[-1][cf.TOKEN_ENV], TOKEN)

    def test_login_mode_runs_the_created_tunnel_without_a_token(self) -> None:
        spawn = FakeSpawn()
        d = cf.Cloudflared(config.load_config({"run_parameters": ["--region=us"]}), spawn=spawn, clock=FakeClock())
        d.start()
        self.assertEqual(spawn.argvs, [])  # nothing to run before the tunnel exists
        self.assertEqual(d.status.state, cf.STATE_SETUP)
        d.poll()
        self.assertEqual(spawn.argvs, [])  # and no restart loop either
        d.tunnel_id = "6ff42ae2-765d-4adf-8112-31c55c1551ef"
        d.start()
        argv = spawn.argvs[-1]
        self.assertEqual(argv[-2:], ["run", "6ff42ae2-765d-4adf-8112-31c55c1551ef"])
        self.assertEqual(argv[argv.index("--config") + 1], cf.INGRESS_PATH)
        self.assertIn("--region=us", argv)
        self.assertNotIn(cf.TOKEN_ENV, spawn.envs[-1])
        d.stop()

    def test_a_token_in_the_environment_never_leaks_into_login_mode(self) -> None:
        spawn = FakeSpawn()
        d = cf.Cloudflared(config.load_config({}), spawn=spawn, clock=FakeClock())
        d.tunnel_id = "6ff42ae2-765d-4adf-8112-31c55c1551ef"
        with unittest.mock.patch.dict("os.environ", {cf.TOKEN_ENV: "stale"}):
            d.start()
        self.assertNotIn(cf.TOKEN_ENV, spawn.envs[-1])
        d.stop()

    def test_loglevel_follows_the_option(self) -> None:
        spawn = FakeSpawn()
        d = daemon(spawn, FakeClock(), log_level="debug")
        d.start()
        self.assertIn("--loglevel=debug", spawn.argvs[-1])


class StatusTest(unittest.TestCase):
    """TunnelStatus bookkeeping."""

    def test_set_returns_true_only_on_change(self) -> None:
        status = cf.TunnelStatus()
        self.assertEqual(status.state, cf.STATE_STARTING)
        self.assertTrue(status.set(cf.STATE_CONNECTED))
        self.assertFalse(status.set(cf.STATE_CONNECTED))
        self.assertTrue(status.set(cf.STATE_DEGRADED, "no edge connection"))
        self.assertEqual(status.last_error, "no edge connection")
        self.assertFalse(status.set(cf.STATE_DEGRADED, "no edge connection"))
        data = status.as_dict()
        self.assertEqual(data["state"], cf.STATE_DEGRADED)
        self.assertEqual(data["last_error"], "no edge connection")
        self.assertIsNotNone(data["last_change"])


class SupervisionTest(unittest.TestCase):
    """Start, watch, restart, stop."""

    def test_connects_when_edges_register(self) -> None:
        spawn = FakeSpawn(
            FakeProcess(
                b"INF Starting tunnel\nINF Registered tunnel connection connIndex=0\nINF Registered tunnel connection connIndex=1\n"
            )
        )
        d = daemon(spawn, FakeClock())
        d.start()
        settled(d)
        self.assertEqual(d.status.state, cf.STATE_CONNECTED)
        self.assertEqual(d.status.connections, 2)
        d.stop()

    def test_stays_starting_without_connections(self) -> None:
        spawn = FakeSpawn(FakeProcess(b"INF Starting tunnel\n"))
        d = daemon(spawn, FakeClock())
        d.start()
        for _ in range(3):
            d.poll()
        self.assertEqual(d.status.state, cf.STATE_STARTING)
        d.stop()

    def test_degraded_after_losing_all_edges(self) -> None:
        spawn = FakeSpawn(
            FakeProcess(
                b"INF Registered tunnel connection connIndex=0\nINF Unregistered tunnel connection connIndex=0\n"
            )
        )
        d = daemon(spawn, FakeClock())
        d.start()
        settled(d)
        d.poll()
        self.assertEqual(d.status.state, cf.STATE_DEGRADED)
        self.assertEqual(d.status.connections, 0)
        d.stop()

    def test_stop_terminates_the_process(self) -> None:
        spawn = FakeSpawn(FakeProcess(b"INF Starting tunnel\n"))
        d = daemon(spawn, FakeClock())
        d.start()
        process = spawn.last
        assert process is not None
        d.stop()
        self.assertTrue(process.terminated)
        self.assertEqual(d.status.state, cf.STATE_STOPPED)
        self.assertEqual(d.status.connections, 0)

    def test_panel_restart_is_immediate(self) -> None:
        spawn = FakeSpawn(FakeProcess(b""), FakeProcess(b""))
        d = daemon(spawn, FakeClock())
        d.start()
        self.assertEqual(d.status.restarts, 0)
        d.restart()
        self.assertEqual(d.status.restarts, 1)
        self.assertEqual(len(spawn.argvs), 2)
        d.stop()

    def test_version_from_the_log(self) -> None:
        spawn = FakeSpawn(FakeProcess(b"INF cloudflared version 2026.10.7 (commit 4f2e)\n"))
        d = daemon(spawn, FakeClock())
        d.start()
        settled(d, until=lambda: d.status.version is not None)
        self.assertEqual(d.status.version, "2026.10.7")
        d.stop()

    def test_connections_come_from_the_local_metrics_page(self) -> None:
        spawn = FakeSpawn(FakeProcess(b"INF Registered tunnel connection connIndex=0\n"))
        page = "cloudflared_tunnel_total_requests 7\ncloudflared_tunnel_ha_connections 4\n"

        def metrics() -> str | None:
            return page

        d = daemon(spawn, FakeClock(), metrics=metrics)
        d.start()
        settled(d)
        self.assertEqual(d.status.connections, 4)  # the gauge wins over the log count
        d.stop()

    def test_a_metrics_page_without_the_gauge_keeps_the_log_count(self) -> None:
        spawn = FakeSpawn(
            FakeProcess(b"INF Registered tunnel connection connIndex=0\nINF Registered tunnel connection connIndex=1\n")
        )

        def metrics() -> str | None:
            return "# no gauge on this page\n"

        d = daemon(spawn, FakeClock(), metrics=metrics)
        d.start()
        settled(d)
        self.assertEqual(d.status.connections, 2)
        d.stop()

    def test_ha_connections_parsing(self) -> None:
        self.assertIsNone(cf._ha_connections(None))
        self.assertIsNone(cf._ha_connections("go_goroutines 3\n"))
        self.assertEqual(cf._ha_connections("cloudflared_tunnel_ha_connections 9\n"), 9)


class RestartTest(unittest.TestCase):
    """Crash handling: backoff and bad tokens."""

    def test_restart_after_the_backoff(self) -> None:
        clock = FakeClock()
        spawn = FakeSpawn(FakeProcess(b""), FakeProcess(b""))
        d = daemon(spawn, clock)
        d.start()
        spawn.last.returncode = 1  # it crashed
        d.poll()  # notices the exit, schedules a restart in 1s
        self.assertEqual(d.status.state, cf.STATE_STOPPED)
        self.assertIn("exited (code 1)", d.status.last_error)
        clock.now += 0.9
        d.poll()
        self.assertEqual(len(spawn.argvs), 1)  # not yet
        clock.now += 0.2
        d.poll()
        self.assertEqual(len(spawn.argvs), 2)  # restarted
        self.assertEqual(d.status.restarts, 1)
        d.stop()

    def test_backoff_grows(self) -> None:
        clock = FakeClock()
        spawn = FakeSpawn(*[FakeProcess(b"") for _ in range(7)])
        d = daemon(spawn, clock)
        d.start()
        delays = []
        for _ in range(6):
            process = spawn.last
            assert process is not None
            process.returncode = 1
            d.poll()  # notices the exit and schedules the next restart
            delays.append(round(d._restart_at - clock.now, 3))
            clock.now = d._restart_at + 0.1
            d.poll()  # restarts
        self.assertEqual(delays, [1.0, 2.0, 5.0, 10.0, 30.0, 60.0])
        d.stop()

    def test_bad_token_is_retried_slowly(self) -> None:
        clock = FakeClock()
        spawn = FakeSpawn(
            FakeProcess(b"FTL Provided Tunnel token is not valid.\n"),
            FakeProcess(b"INF Registered tunnel connection connIndex=0\n"),
        )
        d = daemon(spawn, clock)
        d.start()
        settled(d)
        self.assertEqual(d.status.state, cf.STATE_ERROR)
        self.assertIn("token", (d.status.last_error or "").lower())
        spawn.last.returncode = 1
        d.poll()  # exit noticed: schedules the slow retry
        clock.now += cf.BAD_TOKEN_RETRY_SECONDS - 1
        d.poll()
        self.assertEqual(len(spawn.argvs), 1)  # still waiting
        clock.now += 2
        d.poll()
        self.assertEqual(len(spawn.argvs), 2)  # retried after 5 minutes
        d.stop()

    def test_spawn_failure_waits_and_retries(self) -> None:
        clock = FakeClock()

        def failing(_argv: list[str], _env: dict[str, str]) -> FakeProcess:
            raise OSError("no such file")

        d = daemon(FakeSpawn(), clock)
        d._spawn = failing  # the test injects the failure
        d.start()
        self.assertEqual(d.status.state, cf.STATE_ERROR)
        clock.now += cf.BAD_TOKEN_RETRY_SECONDS
        d.poll()
        self.assertEqual(d.status.restarts, 1)


class BinaryVersionTest(unittest.TestCase):
    """cloudflared --version parsing."""

    def test_parses_the_banner(self) -> None:
        process = FakeProcess(b"cloudflared version 2026.10.7 (commit 4f2e6f1)\n")

        class Run:
            def __call__(self, argv: list[str], _env: dict[str, str]) -> FakeProcess:
                self.argv = argv
                return process

        run = Run()
        self.assertEqual(cf.binary_version(run), "2026.10.7")
        self.assertEqual(run.argv, [cf.CLOUDFLARED, "--version"])

    def test_none_when_the_binary_is_missing(self) -> None:
        def failing(_argv: list[str], _env: dict[str, str]) -> FakeProcess:
            raise OSError("no such file")

        self.assertIsNone(cf.binary_version(failing))


if __name__ == "__main__":
    unittest.main()
