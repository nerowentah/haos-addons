"""A Supervisor that answers from memory, and sample replies (placeholder data only)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import supervisor

GB = 1000**3
STORED: dict[str, object] = {
    "auto_clean": False,
    "auto_clean_backups": False,
    "keep_backups": 2,
    "low_space_percent": 10,
    "notify_on_low_space": True,
    "log_level": "info",
}


def usage_reply(used: int = 20 * GB, total: int = 32 * GB) -> dict[str, object]:
    """``GET /host/disks/default/usage?max_depth=2`` as the Supervisor sends it."""

    return {
        "id": "root",
        "label": "Root",
        "total_bytes": total,
        "used_bytes": used,
        "children": [
            {"id": "system", "label": "System", "used_bytes": 6 * GB},
            {
                "id": "addons_data",
                "label": "Addons data",
                "used_bytes": 5 * GB,
                "children": [
                    {"id": "core_example", "label": "core_example", "used_bytes": 1 * GB},
                    {"id": "a1b2c3d4_example", "label": "a1b2c3d4_example", "used_bytes": 4 * GB},
                ],
            },
            {"id": "addons_config", "label": "Addons configuration", "used_bytes": 1000},
            {"id": "media", "label": "Media", "used_bytes": 1 * GB},
            {"id": "share", "label": "Share", "used_bytes": 0},
            {"id": "backup", "label": "Backup", "used_bytes": 4 * GB},
            {"id": "ssl", "label": "SSL", "used_bytes": 4000},
            {
                "id": "homeassistant",
                "label": "Home Assistant",
                "used_bytes": 3 * GB,
                "children": [
                    {"id": "home-assistant_v2.db", "label": "home-assistant_v2.db", "used_bytes": 2 * GB},
                    {"id": "custom_components", "label": "custom_components", "used_bytes": 1 * GB},
                ],
            },
        ],
    }


def backup_row(  # noqa: PLR0913 - one argument per field of the reply
    slug: str,
    date: str,
    *,
    name: str = "",
    protected: bool = False,
    locations: tuple[str, ...] = (".local",),
    size: int = GB,
) -> dict[str, object]:
    """One entry of ``GET /backups``."""

    return {
        "slug": slug,
        "name": name or f"Backup {slug}",
        "date": date,
        "type": "full",
        "size": round(size / 1048576, 2),
        "size_bytes": size,
        "location": None if ".local" in locations else locations[0],
        "locations": [None if place == ".local" else place for place in locations],
        "protected": protected,
        "location_attributes": {place: {"protected": protected, "size_bytes": size} for place in locations},
        "compressed": True,
    }


def backup_rows() -> list[dict[str, object]]:
    return [
        backup_row("aaaa0001", "2026-01-01T03:00:00+00:00"),
        backup_row("aaaa0002", "2026-01-02T03:00:00+00:00"),
        backup_row("aaaa0003", "2026-01-03T03:00:00+00:00"),
        backup_row("aaaa0004", "2026-01-04T03:00:00+00:00"),
        backup_row("bbbb0001", "2025-12-01T03:00:00+00:00", protected=True, name="Protected one"),
        backup_row("cccc0001", "2025-12-02T03:00:00+00:00", locations=("example_share",), name="Share only"),
        backup_row("dddd0001", "2025-12-03T03:00:00+00:00", locations=(".local", "example_share"), name="Both"),
    ]


class FakeSupervisor(supervisor.Supervisor):
    """Answers the calls the app makes and records every one of them."""

    def __init__(self) -> None:
        super().__init__("placeholder-token")
        self.stored = copy.deepcopy(STORED)
        self.reachable = True
        self.usage: dict[str, object] | None = usage_reply()
        self.backups = backup_rows()
        self.purge_error: str | None = None
        self.calls: list[tuple[str, str, object, float]] = []
        self.written: list[dict[str, object]] = []
        self.sent: list[tuple[str, str]] = []

    def _request(
        self,
        url: str,
        method: str = "GET",
        body: object | None = None,
        timeout: float = supervisor.HTTP_TIMEOUT,
    ) -> object:
        path = url.removeprefix(supervisor.SUPERVISOR_API)
        self.calls.append((method, path, body, timeout))
        if path.startswith("/host/disks/default/usage"):
            if self.usage is None:
                raise supervisor.SupervisorError(f"HTTP 500 from GET {path}")
            return {"result": "ok", "data": self.usage}
        if (method, path) == ("GET", "/backups"):
            return {"result": "ok", "data": {"backups": copy.deepcopy(self.backups)}}
        if (method, path) == ("GET", "/addons"):
            return {"data": {"addons": [{"slug": "a1b2c3d4_example", "name": "Example App"}]}}
        if method == "DELETE" and path.startswith("/backups/"):
            slug = path.removeprefix("/backups/")
            self.backups = [row for row in self.backups if row["slug"] != slug]
            return {"result": "ok"}
        if path == "/core/api/services/recorder/purge":
            if self.purge_error:
                raise supervisor.SupervisorError(self.purge_error)
            return []
        raise supervisor.SupervisorError(f"HTTP 404 from {method} {path}")

    def deleted(self) -> list[tuple[str, object]]:
        """(path, body) of every DELETE."""

        return [(path, body) for method, path, body, _timeout in self.calls if method == "DELETE"]

    def purges(self) -> list[object]:
        """The body of every recorder.purge call."""

        return [body for _method, path, body, _timeout in self.calls if path.endswith("/recorder/purge")]

    def fetch_options(self) -> dict[str, object]:
        if not self.reachable:
            raise supervisor.SupervisorError("unreachable")
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(copy.deepcopy(options))
        self.stored = copy.deepcopy(options)

    def discover_mobile_targets(self) -> list[str] | None:
        return ["mobile_app_example_phone"]

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.sent.append((title, message))
