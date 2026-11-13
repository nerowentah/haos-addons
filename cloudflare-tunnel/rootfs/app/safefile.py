"""Atomic file writes with explicit permissions (shared by the apps in this repo).

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PRIVATE = 0o600  # owner only: state, credentials, private keys
PUBLIC = 0o644  # readable by other apps: certificates, status files


def atomic_write(path: Path, data: bytes | str, mode: int = PRIVATE) -> bool:
    """Replace ``path`` with ``data`` in one step; False when it already held exactly that.

    The data goes to a temporary file with an unpredictable name in the same
    folder (created owner-only), is flushed to disk, gets ``mode`` and is then
    renamed over ``path``: a reader sees the old file or the new one, never a
    partial one, and the content is never readable with wider permissions than
    ``mode``. Raises OSError when the file cannot be written.
    """

    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        same_mode = os.name != "posix" or (path.stat().st_mode & 0o777) == mode  # Windows (tests) has no modes
        if same_mode and path.read_bytes() == payload:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
