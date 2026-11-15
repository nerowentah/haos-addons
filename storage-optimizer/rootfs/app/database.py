"""The recorder database: how big it is, and asking Home Assistant to purge or repack it.

The database files are never opened, changed or deleted here: their size is
read from the folder listing, and the only thing sent to Home Assistant is the
``recorder.purge`` service call. Home Assistant does the work itself.

A repack (SQLite ``VACUUM``) of a large database can take longer than the five
minutes the Supervisor waits for Home Assistant's answer, and the call can
come back before the work is done. So the answer is not trusted either way:
:class:`Watch` follows the size of the database and reports what really
happened once it has stopped changing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from disk import human
from supervisor import Supervisor, SupervisorError

ROOT = Path(os.environ.get("STORAGE_OPTIMIZER_HA_CONFIG", "/homeassistant"))
DB_NAME = "home-assistant_v2.db"
PURGE_TIMEOUT = 310  # just past the Supervisor's own 300 s limit for a call to Home Assistant
POLL_SECONDS = 30
SETTLE_SECONDS = 180  # the size has not moved for this long after it changed: the work is done
IDLE_SECONDS = 900  # the size never moved: nothing was freed (or the purge had nothing to do)
MAX_SECONDS = 4 * 3600
_UNFINISHED = ("timed out", "HTTP 502", "HTTP 504")  # the call ran out of time; the work goes on


class DatabaseError(RuntimeError):
    """Home Assistant refused the purge."""


@dataclass(frozen=True, slots=True)
class DbSize:
    """Size of the recorder database on disk (main file, write-ahead log, shared memory file)."""

    main: int
    wal: int = 0
    shm: int = 0

    @property
    def total(self) -> int:
        return self.main + self.wal + self.shm


def _file_size(path: Path) -> int | None:
    try:
        return path.lstat().st_size
    except OSError:
        return None


def size(root: Path = ROOT) -> DbSize | None:
    """The database's size, or None when there is no SQLite database here (MariaDB, PostgreSQL...)."""

    main = _file_size(root / DB_NAME)
    if main is None:
        return None
    return DbSize(main, _file_size(root / f"{DB_NAME}-wal") or 0, _file_size(root / f"{DB_NAME}-shm") or 0)


def purge(supervisor: Supervisor, *, repack: bool) -> bool:
    """Ask Home Assistant to purge the recorder; True when it confirmed, False when the call ran out of time.

    Without ``keep_days`` Home Assistant uses its own recorder setting. Raises DatabaseError
    when Home Assistant refuses (no recorder, or it is not running).
    """

    try:
        supervisor.core_post_slow("/services/recorder/purge", {"repack": repack}, PURGE_TIMEOUT)
    except SupervisorError as err:
        if any(sign in str(err) for sign in _UNFINISHED):
            return False
        raise DatabaseError(f"Home Assistant did not start the purge: {err}") from err
    return True


@dataclass(slots=True)
class Watch:
    """Follows the database size after a purge until it stops changing, then says what happened."""

    label: str  # "Purge" or "Purge and repack"
    repack: bool
    before: int
    started: float
    last_size: int
    last_change: float
    changed: bool = False
    next_poll: float = 0.0

    @classmethod
    def start(cls, label: str, before: int, now: float, *, repack: bool) -> Watch:
        return cls(label, repack, before, now, before, now, next_poll=now + POLL_SECONDS)

    def update(self, now: float, current: int | None) -> tuple[str, str] | None:
        """Feed the current size; (message, tone) once the outcome is known, None while it is not."""

        if current is None:
            return f"{self.label}: the database is no longer where it was; nothing more to report", "warn"
        if current != self.last_size:
            self.last_size, self.last_change, self.changed = current, now, True
        elapsed = now - self.started
        if self.changed and now - self.last_change >= SETTLE_SECONDS:
            return self._outcome(current)
        if not self.changed and elapsed >= IDLE_SECONDS:
            return self._outcome(current)
        if elapsed >= MAX_SECONDS:
            message = (
                f"{self.label}: Home Assistant is still working on the database after "
                f"{MAX_SECONDS // 3600} hours (now {human(current)}, was {human(self.before)})"
            )
            return message, "warn"
        return None

    def _outcome(self, current: int) -> tuple[str, str]:
        if current < self.before:
            message = (
                f"{self.label} finished: the database went from {human(self.before)} to {human(current)} "
                f"({human(self.before - current)} freed)"
            )
            return message, "ok"
        if self.repack:
            message = (
                f"{self.label}: the database is still {human(current)}. Either there was nothing to free, or the "
                "repack did not run (it needs free space about the size of the database); see Home Assistant's log"
            )
            return message, "warn"
        message = (
            f"{self.label} finished: the file is still {human(current)}. That is expected: without a repack the "
            "freed space stays inside the file and is reused for new data"
        )
        return message, "neutral"
