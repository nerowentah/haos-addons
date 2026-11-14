"""Atomic JSON persistence in /data (survives add-on restarts and updates).

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from safefile import atomic_write

_LOGGER = logging.getLogger(__name__)

STATE_PATH = Path("/data/state.json")


class Store:
    """A small JSON document saved atomically (write temp file, then rename), readable by the app only."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.data: dict[str, object] = {}
        self.dirty = False

    def load(self) -> None:
        """Load the document; a missing or corrupt file starts empty."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            _LOGGER.warning("State file %s is unreadable; starting fresh", self.path)
            return
        if isinstance(raw, dict):
            self.data = raw

    def section(self, name: str) -> dict[str, object]:
        """Return (creating if needed) a top-level mapping."""

        value = self.data.get(name)
        if not isinstance(value, dict):
            value = {}
            self.data[name] = value
        return value

    def save(self) -> None:
        """Write the document if it changed."""

        if not self.dirty:
            return
        try:
            atomic_write(self.path, json.dumps(self.data, separators=(",", ":")))
            self.dirty = False
        except OSError:
            _LOGGER.exception("Could not save state to %s", self.path)
