"""The Supervisor's disk usage report, as typed values, plus the free-space history for the chart.

Nothing here touches the disk: the numbers come from ``GET /host/disks/default/usage``
(the Supervisor walks the folders) and the history lives in the app's own state file.
"""

from __future__ import annotations

from dataclasses import dataclass

USAGE_PATH = "/host/disks/default/usage?max_depth=2"  # depth 2: one level below each area
HISTORY_DAYS = 30
ADDONS_DATA = "addons_data"
HOMEASSISTANT = "homeassistant"
SYSTEM = "system"
# The areas the Supervisor reports, with the names Home Assistant's own Storage page uses.
LABELS: dict[str, str] = {
    SYSTEM: "System",
    ADDONS_DATA: "App data",
    "addons_config": "App configuration",
    "media": "Media",
    "share": "Share",
    "backup": "Backups",
    "ssl": "SSL",
    HOMEASSISTANT: "Home Assistant",
    # The same areas under the names the newer Supervisor API uses.
    "apps_data": "App data",
    "apps_config": "App configuration",
}
ICONS: dict[str, str] = {
    SYSTEM: "mdi:harddisk",
    ADDONS_DATA: "mdi:puzzle",
    "apps_data": "mdi:puzzle",
    "addons_config": "mdi:cog",
    "apps_config": "mdi:cog",
    "media": "mdi:folder",
    "share": "mdi:folder",
    "backup": "mdi:archive",
    "ssl": "mdi:certificate",
    HOMEASSISTANT: "mdi:view-dashboard",
}
_UNITS = ("KB", "MB", "GB", "TB")
_GB = 1000**3


class DiskError(ValueError):
    """The Supervisor's usage report is not in the expected shape."""


@dataclass(frozen=True, slots=True)
class Bucket:
    """One area of the disk (or one folder inside it) and what it holds."""

    id: str
    label: str
    used: int
    children: tuple[Bucket, ...] = ()


@dataclass(frozen=True, slots=True)
class Usage:
    """The whole disk: its size, what is used, and the areas, largest first."""

    total: int
    used: int
    buckets: tuple[Bucket, ...]

    @property
    def free(self) -> int:
        return max(self.total - self.used, 0)

    @property
    def free_fraction(self) -> float:
        return self.free / self.total if self.total > 0 else 0.0

    def bucket(self, *ids: str) -> Bucket | None:
        """The area with one of these ids (the old and the new API name of the same area)."""

        return next((b for b in self.buckets if b.id in ids), None)


def _size(value: object) -> int:
    """A byte count from the report (0 for anything that is not a non-negative number)."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _bucket(node: object, *, top: bool) -> Bucket | None:
    if not isinstance(node, dict):
        return None
    raw_id, raw_label = node.get("id"), node.get("label")
    ident = str(raw_id if raw_id not in (None, "") else raw_label or "")
    if not ident:
        return None
    label = LABELS.get(ident) if top else None
    children = node.get("children")
    parsed = [_bucket(child, top=False) for child in children] if isinstance(children, list) else []
    return Bucket(
        id=ident,
        label=label or str(raw_label or ident),
        used=_size(node.get("used_bytes")),
        children=tuple(sorted((c for c in parsed if c is not None), key=lambda c: c.used, reverse=True)),
    )


def parse_usage(data: object) -> Usage:
    """Read the report. An area with an unknown id is kept under its own label, never dropped."""

    if not isinstance(data, dict):
        raise DiskError("The Supervisor returned no disk usage")
    total = _size(data.get("total_bytes"))
    if total <= 0:
        raise DiskError("The Supervisor did not report the size of the disk")
    children = data.get("children")
    parsed = [_bucket(child, top=True) for child in children] if isinstance(children, list) else []
    return Usage(
        total=total,
        used=min(_size(data.get("used_bytes")), total),
        buckets=tuple(sorted((b for b in parsed if b is not None), key=lambda b: b.used, reverse=True)),
    )


def human(size: int) -> str:
    """``1536000000`` -> ``1.5 GB`` (decimal units, like Home Assistant's Storage page)."""

    value = float(max(size, 0))
    if value < 1000:  # noqa: PLR2004 - below one kilobyte
        return f"{int(value)} B"
    index = -1
    while value >= 1000 and index < len(_UNITS) - 1:  # noqa: PLR2004 - next unit up
        value /= 1000
        index += 1
    unit = _UNITS[index]
    return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"  # noqa: PLR2004 - three digits


def percent(part: int, whole: int) -> str:
    """``part`` as a share of ``whole``: ``12%``, ``<1%`` for a sliver, ``0%`` for nothing."""

    if whole <= 0 or part <= 0:
        return "0%"
    share = part / whole * 100
    return "<1%" if share < 1 else f"{share:.0f}%"


def is_low(usage: Usage, limit_percent: int) -> bool:
    """Whether less than ``limit_percent`` of the disk is free."""

    return usage.free_fraction * 100 < limit_percent


def add_sample(history: dict[str, object], day: str, usage: Usage) -> bool:
    """Record today's free space (``day`` is YYYY-MM-DD) and drop days beyond the chart; True when changed."""

    sample = {"free": usage.free, "total": usage.total}
    changed = history.get(day) != sample
    history[day] = sample
    for old in sorted(history)[:-HISTORY_DAYS]:
        del history[old]
        changed = True
    return changed


def chart_bars(history: dict[str, object]) -> list[tuple[str, float]]:
    """(MM-DD, free GB) per recorded day, oldest first; rows that are not samples are skipped."""

    bars: list[tuple[str, float]] = []
    for day in sorted(history)[-HISTORY_DAYS:]:
        sample = history[day]
        if isinstance(sample, dict) and _size(sample.get("free")) > 0:
            bars.append((day[5:], round(_size(sample.get("free")) / _GB, 2)))
    return bars
