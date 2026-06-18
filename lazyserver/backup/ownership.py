"""Snapshot + reapply owner/group/mode around an edit (FR-1.11).

Editors that save by writing a temp file and `rename(2)`ing it over the
original silently inherit the running process's uid/gid and the
umask-default mode — so a root-run session editing `/etc/named.conf`
otherwise leaves it `root:root` instead of `bind:bind`. We stat() before
the editor and reapply chown/chmod after, idempotently. The reapply is
a no-op when nothing changed.

Pure stdlib; no privilege check here — `check_root_privilege` at startup
(NFR-3) is the gate. Best-effort: chown/chmod failures are logged, not
raised, because losing the post-edit content over an ownership reset
would be a worse outcome than a stale mode the user can see and fix.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("lazyserver.backup.ownership")


@dataclass(frozen=True)
class StatSnapshot:
    """Owner/group/mode captured before an edit."""

    uid: int
    gid: int
    mode: int  # the permission bits only, st_mode & 0o7777


def snapshot(path: Path) -> StatSnapshot | None:
    """Capture uid/gid/mode of `path`, or return None if it doesn't exist."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return StatSnapshot(uid=st.st_uid, gid=st.st_gid, mode=st.st_mode & 0o7777)


def reapply(path: Path, snap: StatSnapshot | None) -> bool:
    """Reapply `snap` to `path`. Returns True iff anything actually changed.

    None snap means the file did not exist before; FR-1.7's create flow
    owns initial ownership in that case, so there is nothing to restore.
    Missing file after the edit (the user deleted it) is also a no-op.
    """
    if snap is None:
        return False
    try:
        st = path.stat()
    except FileNotFoundError:
        return False

    changed = False
    current_mode = st.st_mode & 0o7777
    if current_mode != snap.mode:
        try:
            os.chmod(path, snap.mode)
            changed = True
        except PermissionError as exc:
            log.warning("chmod %04o on %s failed: %s", snap.mode, path, exc)
    if st.st_uid != snap.uid or st.st_gid != snap.gid:
        try:
            os.chown(path, snap.uid, snap.gid)
            changed = True
        except PermissionError as exc:
            log.warning(
                "chown %d:%d on %s failed: %s", snap.uid, snap.gid, path, exc
            )
    return changed
