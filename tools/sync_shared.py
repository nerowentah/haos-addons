"""Copy the shared modules in ``shared/app`` into every app that uses them.

Each app is built from its own folder (the Docker build context), so the
shared code has to live in every image. ``shared/app`` is the only place to
edit it; this script writes the copies, and ``--check`` (run by CI) fails when
a copy differs from its source.

Usage: python tools/sync_shared.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "shared" / "app"
APP_DIR = Path("rootfs") / "app"

ALL = "*"
WITH_SUPERVISOR = (
    "api-key-manager",
    "cloudflare-tunnel",
    "mobile-device-manager",
    "ranhill-saj-bill-checker",
    "ssl-cert-manager",
    "tnb-usage-checker",
)
# Shared file -> the apps that ship it (ALL: every folder with a config.yaml).
SHARED: dict[str, tuple[str, ...] | str] = {
    "webui.py": ALL,
    "web/index.html": ALL,
    "web/panel.css": ALL,
    "web/panel.js": ALL,
    "web/icons.js": ALL,
    "safefile.py": ALL,
    "jsonval.py": ALL,
    "redact.py": ALL,
    "safehttp.py": ("cloudflare-tunnel", "ssl-cert-manager", "tnb-usage-checker"),
    "store.py": WITH_SUPERVISOR,
    "supervisor.py": WITH_SUPERVISOR,
    "maintenance.py": ("lg-webos-devmode-mgmt", "tnb-usage-checker"),
    "unpaid.py": ("ranhill-saj-bill-checker", "tnb-usage-checker"),
    "reach.py": ("cloudflare-tunnel", "ssl-cert-manager"),
}


def app_names() -> list[str]:
    """Every app folder (one level deep, with a config.yaml)."""

    return sorted(path.parent.name for path in REPO.glob("*/config.yaml"))


def targets() -> list[tuple[Path, Path]]:
    """(source, copy) for every shared file in every app that ships it."""

    apps = app_names()
    pairs: list[tuple[Path, Path]] = []
    for name, wanted in SHARED.items():
        source = SOURCE / name
        if not source.is_file():
            raise SystemExit(f"shared/app/{name} is listed in tools/sync_shared.py but does not exist")
        for app in apps if wanted == ALL else wanted:
            if app not in apps:
                raise SystemExit(f"tools/sync_shared.py names the app '{app}', which has no config.yaml")
            pairs.append((source, REPO / app / APP_DIR / name))
    return pairs


def _text(path: Path) -> bytes:
    """The file with LF line endings (a Windows checkout has CRLF)."""

    return path.read_bytes().replace(b"\r\n", b"\n")


def differing() -> list[Path]:
    """Copies that are missing or differ from their source (line endings aside)."""

    return [copy for source, copy in targets() if not copy.is_file() or _text(copy) != _text(source)]


def unlisted() -> list[Path]:
    """Files in shared/app that the table above does not mention."""

    return sorted(
        path
        for path in SOURCE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.relative_to(SOURCE).as_posix() not in SHARED
    )


def main(argv: list[str]) -> int:
    check = "--check" in argv
    problems = [f"{path.relative_to(REPO).as_posix()} is not listed in tools/sync_shared.py" for path in unlisted()]
    stale = differing()
    if check:
        problems += [f"{path.relative_to(REPO).as_posix()} differs from shared/app" for path in stale]
    else:
        wanted = {copy: source for source, copy in targets()}
        for copy in stale:
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(wanted[copy].read_bytes())
            sys.stdout.write(f"updated {copy.relative_to(REPO).as_posix()}\n")
    for problem in problems:
        sys.stdout.write(f"::error::{problem}\n")
    if problems:
        sys.stdout.write("Edit the files in shared/app, then run: python tools/sync_shared.py\n")
        return 1
    sys.stdout.write(f"{len(targets())} shared copies {'match' if check else 'are up to date'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
