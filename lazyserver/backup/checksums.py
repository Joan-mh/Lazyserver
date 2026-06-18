"""SHA-256 content checksum for managed files (FR-2.1).

`sha256_of(path)` returns the hex digest of the file content, or None if
the file does not exist. Detection is content-based — no mtime, no size.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 64 * 1024


def sha256_of(path: Path) -> str | None:
    """Return the file's SHA-256 hex digest, or None if absent.

    Other OSErrors (permission denied, IO failure) propagate — those are
    not "file absent" and a managed file we cannot read is an actionable
    error the caller should surface, not silently treat as missing.
    """
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
