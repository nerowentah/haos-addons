"""Atomic, owner-only file writes (shared by the apps in this repo).

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Every file is readable and writable by its owner only: state, credentials,
# private keys, and also the certificates and status files in /ssl. Home
# Assistant and the apps that read those run as root, so nothing needs more.
PRIVATE = 0o600
HAS_MODES = os.name == "posix"  # Windows (where the tests also run) has no such permission bits


def atomic_write(path: Path, data: bytes | str) -> bool:
    """Replace ``path`` with ``data`` in one step; False when it already held exactly that.

    The data goes to a temporary file with an unpredictable name in the same
    folder (created owner-only), is flushed to disk and is then renamed over
    ``path``: a reader sees the old file or the new one, never a partial one,
    and the content is never readable by anyone but the owner. A file that
    holds the same data with wider permissions is written again, which
    tightens it. Raises OSError when the file cannot be written.
    """

    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        same_mode = not HAS_MODES or (path.stat().st_mode & 0o777) == PRIVATE
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
        if HAS_MODES:
            os.chmod(tmp, PRIVATE)  # mkstemp already made it owner-only; this states it
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
