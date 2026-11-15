#!/usr/bin/env python3
"""Home Assistant app: HA Storage Optimizer - shows where the disk space went and frees it safely."""

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

import backup as backup_mod
import database
import disk
import local
import redact
from config import Config, ConfigError, apply_log_level, load_config, load_options
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import (
    PanelArgs,
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    action,
    badge,
    chart,
    hero_item,
    initial_options,
    notice,
    progress,
    row,
    stat,
    toggle,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger("storage_optimizer")

VERSION = os.environ.get("STORAGE_OPTIMIZER_VERSION", "dev")
LOOP_SECONDS = 5
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
SCAN_SECONDS = 3600
SCAN_TIMEOUT = 300  # the Supervisor walks every folder; a full disk of small files takes a while
AUTO_CHECK_SECONDS = 600
STORE_SAVE_SECONDS = 60
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "storage_optimizer"
MAX_ROWS = 12  # apps / folders listed before the rest is summed up
MAX_OPEN_TOGGLES = 8  # more backups than this are folded behind a summary line
REPACK_CONFIRM = (
    "Purge and repack the recorder database? Home Assistant rewrites the whole database: it needs free space "
    "about the size of the database while it works, history is not recorded until it is done, and a large "
    "database takes a long time. This app watches the database size and reports the real result here."
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ago(moment: dt.datetime, now: dt.datetime) -> str:
    seconds = max(int((now - moment).total_seconds()), 0)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class JobError(RuntimeError):
    """A clean-up that could not be done; the message is shown in the panel."""


@dataclass(frozen=True, slots=True)
class Scan:
    """Everything the panel shows, read in one go on the worker thread (the panel never waits for a scan)."""

    at: dt.datetime
    usage: disk.Usage | None = None
    usage_error: str = ""
    backups: tuple[backup_mod.Backup, ...] | None = None
    backups_error: str = ""
    db: database.DbSize | None = None
    files: tuple[local.Item, ...] = ()
    app_names: dict[str, str] = field(default_factory=dict)


class App:
    """Scans the disk, runs the clean-ups the user asks for and serves the panel."""

    def __init__(  # noqa: PLR0913 - the tests inject the seams
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        *,
        ha_root: Path = local.ROOT,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.supervisor = supervisor
        self.store = store
        self.ha_root = ha_root
        self.clock = clock
        self.version = VERSION
        self.discovered: list[str] = []
        self.lock = threading.RLock()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.jobs: queue.Queue[tuple[str, Callable[[], str | None], bool]] = queue.Queue()
        self.busy: str | None = None
        self.last_job: tuple[str, str] | None = None  # (message, tone) of the last finished job
        self.scan: Scan | None = None
        self.scan_queued = False
        self.selected: set[str] = set()  # backups ticked for deletion (not saved: a restart clears it)
        self.watch: database.Watch | None = None
        self.next_scan = 0.0
        self.next_auto_check = 0.0
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.next_phone_check = 0.0

    # -- options ------------------------------------------------------------------------
    def view_config(self) -> Config:
        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        old = self.config
        self.config = config
        self.raw_options = raw
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)
            apply_log_level(config.log_level)
        if config.auto_clean and not old.auto_clean:
            _LOGGER.info("Automatic clean-up switched on")
            self.next_auto_check = 0.0
        if config.backups_auto_deleted and not old.backups_auto_deleted:
            _LOGGER.info("Automatic clean-up now also deletes old backups (keeping %d)", config.keep_backups)
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve_options(raw))
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve_options(raw))
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- phones ---------------------------------------------------------------------------
    @property
    def phones(self) -> dict[str, object]:
        return self.store.section("phones")

    def refresh_phones(self) -> None:
        found = self.supervisor.discover_mobile_targets()
        if found is None:
            return
        with self.lock:
            merged = merge_discovered(self.phones, found)
            self.discovered = found
            if merged != self.phones:
                self.store.data["phones"] = merged
                self.store.dirty = True

    def set_phone(self, name: str, value: bool) -> None:
        with self.lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, title: str, message: str, suffix: str = "") -> None:
        if self.config.notify_on_low_space:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- jobs (one worker thread: a scan or a clean-up can take minutes) -------------------
    def enqueue(self, label: str, job: Callable[[], str | None], *, rescan: bool = True) -> None:
        self.jobs.put((label, job, rescan))

    def request_scan(self) -> bool:
        """Queue a scan unless one is already waiting; True when it was queued."""

        with self.lock:
            if self.scan_queued:
                return False
            self.scan_queued = True
        self.next_scan = time.monotonic() + SCAN_SECONDS
        self.enqueue("Scanning the disk", self._scan_job, rescan=False)
        return True

    def _worker(self) -> None:
        while not self.stopping.is_set():
            try:
                label, job, rescan = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self.run_job(label, job, rescan=rescan)

    def run_job(self, label: str, job: Callable[[], str | None], *, rescan: bool = True) -> None:
        """Run one queued job; its outcome goes to the panel's Activity card."""

        self.busy = label
        try:
            message = job()
            if message:
                self.last_job = (message, "ok")
        except (JobError, SupervisorError, backup_mod.BackupError, database.DatabaseError, OSError) as err:
            _LOGGER.error("%s: %s", label, err)  # noqa: TRY400 - the message says it all
            self.last_job = (str(err), "error")
        except Exception:
            _LOGGER.exception("%s failed", label)
            self.last_job = (f"{label} failed; see the app log", "error")
        finally:
            self.busy = None
            if rescan:
                self.request_scan()
            self.wake.set()

    # -- scanning (worker thread) ------------------------------------------------------------
    def _scan_job(self) -> None:
        with self.lock:
            self.scan_queued = False
        self.run_scan()

    def _app_names(self) -> dict[str, str]:
        """Folder name (the app's slug) -> the app's name, for the per-app rows."""

        try:
            rows = self.supervisor.api_get("/addons").get("addons")
        except SupervisorError:
            return {}
        return {
            str(entry["slug"]): str(entry["name"])
            for entry in (rows if isinstance(rows, list) else [])
            if isinstance(entry, dict) and entry.get("slug") and entry.get("name")
        }

    def run_scan(self) -> Scan:
        """Read everything the panel shows. Each part fails on its own, with its own message."""

        usage: disk.Usage | None = None
        usage_error = backups_error = ""
        backups: tuple[backup_mod.Backup, ...] | None = None
        try:
            usage = disk.parse_usage(self.supervisor.api_get(disk.USAGE_PATH, timeout=SCAN_TIMEOUT))
        except (SupervisorError, disk.DiskError) as err:
            usage_error = str(err)
            _LOGGER.warning("Disk usage is not available: %s", err)
        try:
            backups = tuple(backup_mod.fetch(self.supervisor))
        except (SupervisorError, backup_mod.BackupError) as err:
            backups_error = str(err)
            _LOGGER.warning("The backups are not available: %s", err)
        scan = Scan(
            at=self.clock(),
            usage=usage,
            usage_error=usage_error,
            backups=backups,
            backups_error=backups_error,
            db=database.size(self.ha_root),
            files=tuple(local.scan(self.ha_root)),
            app_names=self._app_names(),
        )
        with self.lock:
            self.scan = scan
            if backups is not None:
                self.selected &= {b.slug for b in backups if b.deletable}
            if usage is not None and disk.add_sample(
                self.store.section("history"), f"{scan.at.astimezone():%Y-%m-%d}", usage
            ):
                self.store.dirty = True
        if usage is not None:
            _LOGGER.info(
                "Scan: %s free of %s (%s used)", disk.human(usage.free), disk.human(usage.total), disk.human(usage.used)
            )
            self._check_low_space(usage)
        return scan

    def _check_low_space(self, usage: disk.Usage) -> None:
        low = disk.is_low(usage, self.config.low_space_percent)
        state = self.store.section("low_space")
        if low and not state.get("notified"):
            self.notify(
                "Home Assistant is low on disk space",
                f"{disk.human(usage.free)} free of {disk.human(usage.total)} "
                f"(below {self.config.low_space_percent}%). Open Storage Optimizer to see where it went.",
                "_low",
            )
        if bool(state.get("notified")) != low:
            with self.lock:
                state["notified"] = low
                self.store.dirty = True

    # -- clean-ups (worker thread) -------------------------------------------------------------
    def _purge(self, *, repack: bool) -> str:
        label = "Purge and repack" if repack else "Purge"
        before = database.size(self.ha_root)
        confirmed = database.purge(self.supervisor, repack=repack)
        _LOGGER.info("%s of the recorder database requested (confirmed: %s)", label, confirmed)
        if before is None:
            return f"{label} requested. The database is not a file in the configuration folder, so its size is unknown"
        self.watch = database.Watch.start(label, before.total, time.monotonic(), repack=repack)
        waiting = "" if confirmed else " Home Assistant has not answered within 5 minutes; it is still working."
        return f"{label} started.{waiting} Watching the database size ({disk.human(before.total)}) for the result..."

    def _clean_files(self) -> str:
        result = local.clean(self.ha_root)
        for name in result.cleaned:
            _LOGGER.info("Cleaned %s", name)
        if result.errors and not result.cleaned:
            raise JobError(f"Nothing was cleaned: {'; '.join(result.errors[:3])}")
        if not result.cleaned:
            return "Nothing to clean"
        skipped = f" ({_plural(len(result.errors), 'file')} skipped; see the app log)" if result.errors else ""
        return f"Freed {disk.human(result.freed)} from {_plural(len(result.cleaned), 'file')}{skipped}"

    def _delete_backups(self, slugs: list[str]) -> str:
        freed, done, errors = 0, [], []
        for slug in slugs:
            try:
                gone = backup_mod.delete(self.supervisor, slug)
            except (backup_mod.BackupError, SupervisorError) as err:
                _LOGGER.warning("Backup %s was not deleted: %s", slug, err)
                errors.append(str(err))
                continue
            _LOGGER.info("Deleted the local copy of backup %s (%s)", slug, disk.human(gone.size))
            freed += gone.size
            done.append(gone.name)
            with self.lock:
                self.selected.discard(slug)
        if errors and not done:
            raise JobError(f"No backup was deleted: {'; '.join(errors[:3])}")
        skipped = f"; {_plural(len(errors), 'backup')} skipped: {errors[0]}" if errors else ""
        return f"Deleted {_plural(len(done), 'backup')} ({disk.human(freed)}){skipped}"

    def auto_clean(self) -> str:
        """The daily clean-up. Backups are only touched when both switches are on."""

        config = self.config
        if not config.auto_clean:
            return "Automatic clean-up is off"
        try:
            parts = [self._clean_files()]
        except JobError as err:
            _LOGGER.warning("Automatic clean-up: %s", err)
            parts = ["the log and cache files could not be cleaned"]
        if database.size(self.ha_root) is not None:
            try:
                database.purge(self.supervisor, repack=False)
                parts.append("recorder purge requested")
            except database.DatabaseError as err:
                _LOGGER.warning("Automatic purge: %s", err)
                parts.append("the recorder purge failed")
        if config.backups_auto_deleted:
            chosen = backup_mod.select_auto(backup_mod.fetch(self.supervisor), config.keep_backups)
            if chosen:
                parts.append(self._delete_backups([b.slug for b in chosen]))
        return f"Automatic clean-up: {'; '.join(parts)}"

    def _auto_due(self) -> bool:
        if not self.config.auto_clean:
            return False
        today = f"{self.clock().astimezone():%Y-%m-%d}"
        with self.lock:
            state = self.store.section("auto")
            if state.get("last_run") == today:
                return False
            state["last_run"] = today
            self.store.dirty = True
        return True

    # -- panel callbacks (HTTP thread) -----------------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if kind == "backup" and name:
            with self.lock:
                scan = self.scan
                known = {b.slug for b in (scan.backups or ()) if b.deletable} if scan else set()
                if name not in known:
                    raise PanelError("That backup is no longer listed; reload the page")
                if value:
                    self.selected.add(name)
                else:
                    self.selected.discard(name)
            return
        if toggle_id in ("auto_clean", "auto_clean_backups", "notify_on_low_space"):
            self.update_options(lambda raw: raw.__setitem__(toggle_id, value))
            return
        raise PanelError("Unknown setting")

    def panel_action(self, action_id: str, _args: PanelArgs) -> str | None:
        if action_id == "scan":
            return "Scanning..." if self.request_scan() else "A scan is already waiting"
        if action_id in ("purge", "repack"):
            if self.watch is not None:
                raise PanelError("Home Assistant is still working on the database; wait for the result first")
            repack = action_id == "repack"
            self.enqueue("Asking Home Assistant to purge the recorder", lambda: self._purge(repack=repack))
            return "Asking Home Assistant..."
        if action_id == "clean_files":
            self.enqueue("Cleaning log and cache files", self._clean_files)
            return "Cleaning..."
        if action_id == "delete_backups":
            with self.lock:
                slugs = sorted(self.selected)
            if not slugs:
                raise PanelError("Switch on the backups to delete first")
            self.enqueue(f"Deleting {_plural(len(slugs), 'backup')}", lambda: self._delete_backups(slugs))
            return "Deleting..."
        raise PanelError("Unknown action")

    # -- panel view model ----------------------------------------------------------------------
    def _activity_card(self) -> dict[str, object] | None:
        watch = self.watch
        if not (self.busy or self.last_job or watch):
            return None
        card: dict[str, object] = {"title": "Activity", "icon": "mdi:progress-clock"}
        if self.busy:
            card["badge"] = badge("Working", "neutral")
            card["notice"] = notice(f"{self.busy}...", "neutral")
        elif self.last_job:
            card["notice"] = notice(*self.last_job)
        if watch is not None:
            card["badge"] = badge("Working", "neutral")
            card["links"] = [
                row(
                    f"{watch.label}: watching the database size",
                    f"Was {disk.human(watch.before)}, now {disk.human(watch.last_size)}",
                    icon="mdi:database",
                )
            ]
        return card

    def _overview_card(self, scan: Scan | None, config: Config, now: dt.datetime) -> dict[str, object]:
        card: dict[str, object] = {
            "title": "Overview",
            "icon": "mdi:harddisk",
            "subtitle": "The disk Home Assistant runs on",
            "actions": [action("scan", "Scan now", icon="mdi:refresh")],
        }
        usage = scan.usage if scan else None
        if scan is None:
            card["badge"] = badge("Scanning...", "neutral")
            card["empty"] = "The first scan is running. Large disks take a minute or two."
            return card
        if usage is None:
            card["badge"] = badge("No data", "error")
            card["notice"] = notice(f"The Supervisor did not report disk usage: {scan.usage_error}", "error")
            return card
        low = disk.is_low(usage, config.low_space_percent)
        card["badge"] = badge("Low space", "warn") if low else badge("OK", "ok")
        card["hero"] = [
            hero_item("Free", disk.human(usage.free)),
            hero_item("Used", disk.human(usage.used)),
            hero_item("Total", disk.human(usage.total)),
        ]
        card["progress"] = progress(
            usage.used / usage.total, f"{disk.percent(usage.used, usage.total)} used", f"Scanned {_ago(scan.at, now)}"
        )
        with self.lock:
            bars = disk.chart_bars(self.store.section("history"))
        card["chart"] = chart("Free space, last 30 days", bars, "GB", "One bar per day the app was running")
        if low:
            card["notice"] = notice(
                f"Less than {config.low_space_percent}% of the disk is free. Home Assistant needs room for "
                "updates and backups.",
                "warn",
            )
        return card

    @staticmethod
    def _rows(buckets: tuple[disk.Bucket, ...], whole: int, icon: str, names: dict[str, str]) -> list[object]:
        """One row per folder, largest first; everything past MAX_ROWS is summed into one line."""

        rows: list[object] = [
            row(names.get(b.id, b.label), f"{disk.human(b.used)} · {disk.percent(b.used, whole)}", icon=icon)
            for b in buckets[:MAX_ROWS]
        ]
        rest = buckets[MAX_ROWS:]
        if rest:
            total = sum(b.used for b in rest)
            rows.append(row(f"{len(rest)} more", disk.human(total), icon="mdi:dots-horizontal"))
        return rows

    def _where_cards(self, scan: Scan | None) -> list[dict[str, object]]:
        usage = scan.usage if scan else None
        if scan is None or usage is None:
            return [{"title": "By area", "icon": "mdi:chart-donut", "empty": "Shown after a successful scan."}]
        cards: list[dict[str, object]] = [
            {
                "title": "By area",
                "icon": "mdi:chart-donut",
                "subtitle": "Share of the whole disk, largest first",
                "links": [
                    row(
                        b.label,
                        f"{disk.human(b.used)} · {disk.percent(b.used, usage.total)}",
                        icon=disk.ICONS.get(b.id, "mdi:folder"),
                    )
                    for b in usage.buckets
                ],
            }
        ]
        apps = usage.bucket(disk.ADDONS_DATA, "apps_data")
        if apps is not None and apps.children:
            cards.append(
                {
                    "title": "App data, per app",
                    "icon": "mdi:puzzle",
                    "subtitle": f"{disk.human(apps.used)} in total; share of the app data",
                    "links": self._rows(apps.children, apps.used, "mdi:puzzle", scan.app_names),
                }
            )
        home = usage.bucket(disk.HOMEASSISTANT)
        if home is not None and home.children:
            cards.append(
                {
                    "title": "Home Assistant, per folder",
                    "icon": "mdi:folder",
                    "subtitle": f"{disk.human(home.used)} in total; share of the configuration folder",
                    "links": self._rows(home.children, home.used, "mdi:folder", {}),
                }
            )
        return cards

    def _database_card(self, scan: Scan | None) -> dict[str, object]:
        card: dict[str, object] = {
            "title": "Recorder database",
            "icon": "mdi:database",
            "subtitle": "History and statistics. Home Assistant does the work; this app only asks",
        }
        db = scan.db if scan else None
        if db is None:
            card["badge"] = badge("Not found", "off")
            card["empty"] = (
                "No home-assistant_v2.db in the configuration folder: the recorder uses another database "
                "(MariaDB, PostgreSQL) or is not set up."
            )
            return card
        busy = self.watch is not None
        reason = "Home Assistant is still working on the database"
        card["badge"] = badge("Working", "neutral") if busy else badge(disk.human(db.total), "neutral")
        card["stats"] = [stat("Database", disk.human(db.main)), stat("Write-ahead log", disk.human(db.wal + db.shm))]
        card["actions"] = [
            action(
                "purge",
                "Purge",
                confirm="Delete recorder history older than the number of days set in Home Assistant "
                "(purge_keep_days, 10 by default)? Long-term statistics are kept.",
                disabled=busy,
                reason=reason,
                icon="mdi:delete-sweep",
            ),
            action(
                "repack", "Purge and repack", confirm=REPACK_CONFIRM, disabled=busy, reason=reason, icon="mdi:database"
            ),
        ]
        card["notice"] = notice(
            "A purge alone rarely makes the file smaller: the freed space is reused. A repack shrinks it, but "
            "Home Assistant can take longer than the 5 minutes its answer is waited for, so the result shown "
            "here comes from watching the size of the database.",
            "neutral",
        )
        return card

    def _backups_card(self, scan: Scan | None, config: Config) -> dict[str, object]:
        card: dict[str, object] = {
            "title": "Backups",
            "icon": "mdi:archive",
            "subtitle": "Backups on this system that are not password-protected",
        }
        backups = scan.backups if scan else None
        if scan is None or backups is None:
            card["empty"] = (
                f"The backups are not available: {scan.backups_error}" if scan else "Listed after the first scan."
            )
            return card
        deletable = [b for b in backups if b.deletable]
        with self.lock:
            selected = {b.slug for b in deletable} & self.selected
        chosen = [b for b in deletable if b.slug in selected]
        others = len(backups) - len(deletable)
        notes = []
        if others:
            notes.append(f"{_plural(others, 'backup')} not listed (password-protected, or stored elsewhere only).")
        if config.backups_auto_deleted:
            notes.append(f"Automatic clean-up deletes the backups listed here except the newest {config.keep_backups}.")
        else:
            notes.append("Backups are never deleted automatically.")
        card["notice"] = notice(" ".join(notes), "warn" if config.backups_auto_deleted else "neutral")
        if not deletable:
            card["empty"] = "No local, unprotected backups."
            return card
        card["stats"] = [
            stat("Listed here", len(deletable)),
            stat("Their size", disk.human(sum(b.size for b in deletable))),
            stat("Selected", disk.human(sum(b.size for b in chosen)) if chosen else "-"),
        ]
        card["toggles"] = [
            toggle(
                f"backup:{b.slug}",
                b.name,
                b.slug in selected,
                " · ".join(
                    part
                    for part in (
                        f"{b.date.astimezone():%Y-%m-%d %H:%M}" if b.date else "",
                        b.kind,
                        disk.human(b.size),
                        f"also on {', '.join(b.elsewhere)} (kept)" if b.elsewhere else "",
                    )
                    if part
                ),
                icon="mdi:archive",
            )
            for b in deletable
        ]
        if len(deletable) > MAX_OPEN_TOGGLES:
            card["toggles_label"] = "Choose backups to delete"
        card["actions"] = [
            action(
                "delete_backups",
                f"Delete selected ({len(chosen)})",
                confirm=f"Delete {_plural(len(chosen), 'backup')} ({disk.human(sum(b.size for b in chosen))}) "
                "from this system? This cannot be undone.",
                danger=True,
                disabled=not chosen,
                reason="Switch on the backups to delete first",
                icon="mdi:delete",
            )
        ]
        return card

    @staticmethod
    def _files_card(scan: Scan | None) -> dict[str, object] | None:
        """Only shown when there is something to clean (Home Assistant 2025.11 and later keep no log file)."""

        items = list(scan.files) if scan else []
        if not items:
            return None
        total = sum(item.size for item in items)
        return {
            "title": "Logs and caches",
            "icon": "mdi:file-document",
            "subtitle": "In the configuration folder. Home Assistant makes them again when it needs them",
            "badge": badge(disk.human(total), "neutral"),
            "links": [
                row(label, f"{_plural(count, 'file')} · {disk.human(size)}", icon="mdi:file-document")
                for label, count, size in local.summary(items)
            ],
            "actions": [
                action(
                    "clean_files",
                    "Clean",
                    confirm=f"Empty the log files and delete the cached files listed here ({disk.human(total)})?",
                    disabled=total == 0,
                    reason="Nothing to clean",
                    icon="mdi:broom",
                )
            ],
        }

    @staticmethod
    def _not_cleaned_card() -> dict[str, object]:
        return {
            "title": "Not cleaned here",
            "icon": "mdi:shield-lock",
            "subtitle": "Shown above so you know where the space is, but never changed by this app",
            "links": [
                row(
                    "System",
                    "The operating system, Docker images and containers. Update or remove apps you do not use "
                    "in Settings → Apps",
                    icon="mdi:harddisk",
                ),
                row(
                    "Other apps' data",
                    "Each app owns its data. Clean it up in that app, or uninstall the app",
                    icon="mdi:puzzle",
                ),
                row(
                    "Media, share and SSL",
                    "Your own files. Remove them with the File editor, Samba or SSH apps",
                    icon="mdi:folder",
                ),
                row("Host journal", "The operating system's own log is left untouched", icon="mdi:script-text"),
            ],
        }

    def panel_state(self) -> dict[str, object]:
        """The view model, built from the last scan: nothing here waits for the Supervisor or the disk."""

        config = self.view_config()
        now = self.clock()
        scan = self.scan
        activity = self._activity_card()
        files_card = self._files_card(scan)
        settings = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "The numbers are edited in the Configuration tab",
            "stats": [
                stat("Low space below", config.low_space_percent, "%"),
                stat("Backups kept", config.keep_backups),
                stat("Log level", config.log_level),
            ],
            "toggles": [
                toggle(
                    "auto_clean",
                    "Automatic clean-up",
                    config.auto_clean,
                    "Once a day: clean the log and cache files and purge the recorder (no repack)",
                    icon="mdi:broom",
                ),
                toggle(
                    "auto_clean_backups",
                    "Also delete old backups",
                    config.auto_clean_backups,
                    f"With automatic clean-up: keep the newest {config.keep_backups} local, unprotected "
                    "backups and delete the older ones",
                    disabled=not config.auto_clean,
                    icon="mdi:archive",
                ),
                toggle(
                    "notify_on_low_space",
                    "Notifications",
                    config.notify_on_low_space,
                    f"When less than {config.low_space_percent}% of the disk is free",
                    icon="mdi:bell",
                ),
            ],
        }
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_low_space,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        clean_cards = [self._database_card(scan), self._backups_card(scan, config)]
        if files_card:
            clean_cards.append(files_card)
        clean_cards.append(self._not_cleaned_card())
        sections = [
            {
                "title": "Overview",
                "icon": "mdi:harddisk",
                "cards": [*([activity] if activity else []), self._overview_card(scan, config, now)],
            },
            {"title": "Where the space is", "icon": "mdi:chart-donut", "cards": self._where_cards(scan)},
            {"title": "Clean up", "icon": "mdi:broom", "cards": clean_cards},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]},
        ]
        usage = scan.usage if scan else None
        free = f"{disk.human(usage.free)} free" if usage else "scanning"
        return {
            "title": "HA Storage Optimizer",
            "subtitle": f"Version {self.version} · {free}",
            "sections": sections,
        }

    # -- loop -----------------------------------------------------------------------------------
    def poll_watch(self, now: float) -> None:
        """Report the result of a purge once the database size has settled (loop thread: one stat call)."""

        watch = self.watch
        if watch is None or now < watch.next_poll:
            return
        watch.next_poll = now + database.POLL_SECONDS
        current = database.size(self.ha_root)
        outcome = watch.update(now, current.total if current else None)
        if outcome is not None:
            _LOGGER.info("%s", outcome[0])
            self.last_job = outcome
            self.watch = None
            self.request_scan()

    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        if now >= self.next_scan:
            self.request_scan()
        self.poll_watch(now)
        if now >= self.next_auto_check:
            self.next_auto_check = now + AUTO_CHECK_SECONDS
            if self._auto_due():
                self.enqueue("Automatic clean-up", self.auto_clean)

    def run(self) -> None:
        worker = threading.Thread(target=self._worker, daemon=True, name="jobs")
        worker.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS or self.store.dirty:
                    with self.lock:
                        self.store.save()
                    last_save = time.monotonic()
                self.wake.wait(LOOP_SECONDS)
        finally:
            self.store.save()
            _LOGGER.info("Stopped")


SETUP_HELP = (
    "Open Settings → Apps → HA Storage Optimizer → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """# Everything is optional. Nothing is cleaned automatically unless you switch it on:
auto_clean: false
auto_clean_backups: false
keep_backups: 3
low_space_percent: 10"""


def _load_config(
    supervisor: Supervisor, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve_options(raw))

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        view = SetupView("HA Storage Optimizer", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(supervisor.fetch_options, validate, view, stop)
        if result is None:
            return None
        raw, config = result
        if not isinstance(config, Config):
            raise TypeError("the options were validated into something that is not a Config") from None
        _LOGGER.info("Configuration is valid now; starting")
        return raw, config


def main() -> None:
    merge_s6_environment()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    redact.install()  # registered secrets (the Supervisor token...) never reach the log
    _LOGGER.info("Starting HA Storage Optimizer %s", VERSION)
    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)
    supervisor = Supervisor(token)
    stop = threading.Event()
    router = PanelRouter()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        stop.set()
        if isinstance(router.target, App):
            router.target.stopping.set()
            router.target.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server = PanelServer(WEB_DIR, router.get_state, router.on_toggle, router.on_action)
    server.start()
    try:
        secrets = SecretRefs()
        loaded = _load_config(supervisor, secrets, router, stop)
        if loaded is None:
            return
        raw_options, config = loaded
        apply_log_level(config.log_level)
        store = Store()
        store.load()
        app = App(config, supervisor, store, raw_options)
        app.secrets = secrets
        router.target = app
        if stop.is_set():
            return
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
