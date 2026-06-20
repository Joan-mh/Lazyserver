"""Ownership-aware filesystem helpers for the backup layer.

LazyServer runs as root (NFR-3), so any directory we mkdir or file we
write defaults to root-owned. The backup store is the student's data:
their ledger, their archive, what they git-clone and rsync between
machines. Leaving it root-owned would lock them out of their own
backups (mirror of the FR-7.2 config.toml rule).

Two helpers, both target-user-aware:

  ``mkdir_owned_chain``  Create missing directories from ``stop_at``
                         (the configured backup_store root, never
                         touched) down to ``leaf``; chown each newly-
                         created directory to the target user.

  ``write_owned``        Atomically write content to a path, with the
                         final file landing as target_user:target_group
                         and a controlled mode.

``target_user=None`` is supported for headless tests and for sessions
without a backup store configured — chown becomes a no-op and the
process owner inherits the files. Production wiring always passes a
TargetUser.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..platform.user import TargetUser

log = logging.getLogger("lazyserver.backup.fsutil")


def mkdir_owned_chain(
    leaf: Path,
    target_user: TargetUser | None,
    *,
    stop_at: Path,
    dir_mode: int = 0o755,
) -> None:
    """Ensure ``leaf`` exists; chown any directory we create.

    Walks parents upward from ``leaf``, collecting those that don't
    exist; creates them top-down so each chown lands on a directory
    that the kernel sees as already-owned-by-us. ``stop_at`` is the
    configured backup store root — we never alter its ownership or
    mode, by design (the user pre-created it, that's their choice).

    Raises ``ValueError`` if ``leaf`` is not under ``stop_at``.
    """
    leaf = leaf.resolve() if leaf.is_absolute() else leaf
    stop_at = stop_at.resolve() if stop_at.is_absolute() else stop_at
    try:
        leaf.relative_to(stop_at)
    except ValueError as exc:
        raise ValueError(
            f"mkdir_owned_chain refuses to create {leaf} outside backup store "
            f"root {stop_at}"
        ) from exc

    to_create: list[Path] = []
    cursor = leaf
    while cursor != stop_at:
        if cursor.exists():
            break
        to_create.append(cursor)
        cursor = cursor.parent

    for d in reversed(to_create):
        d.mkdir(mode=dir_mode)
        _chown(d, target_user)


def ensure_owned_dir(
    path: Path,
    target_user: TargetUser | None,
    *,
    dir_mode: int = 0o755,
) -> bool:
    """Ensure `path` exists as a directory; chown what we create.

    Returns True iff this call created `path` (the announcement
    trigger). A pre-existing `path` is left strictly untouched — same
    rule as `mkdir_owned_chain`'s `stop_at`: a directory the student
    pre-created is their deliberate ownership choice, not ours to
    override. Intermediate parents that we mkdir on the way down are
    chowned along with the leaf, so a student doing `git clone` or
    `rsync` on the resulting tree owns all of it, not just the leaves.

    Used to materialise the backup store root: previously a silent
    `mkdir(parents=True, exist_ok=True)` from a root-run session left
    the store root root-owned, which broke the "student fully owns
    their backups" guarantee that the rest of the store layer upholds
    (mkdir_owned_chain, write_owned).
    """
    if path.exists():
        return False
    to_create: list[Path] = []
    cursor = path
    while not cursor.exists():
        to_create.append(cursor)
        cursor = cursor.parent
    for d in reversed(to_create):
        d.mkdir(mode=dir_mode)
        _chown(d, target_user)
    return True


def write_owned(
    path: Path,
    content: bytes,
    target_user: TargetUser | None,
    *,
    file_mode: int = 0o644,
) -> None:
    """Write ``content`` to ``path`` atomically and with the right owner.

    Strategy: write to ``<path>.tmp``, chown + chmod the temp, then
    ``os.replace`` into place. The replace is atomic on the same
    filesystem, so a crash mid-write leaves either the old file intact
    or the new file fully written with the correct ownership — never a
    half-written or root-owned final.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fp:
        fp.write(content)
    os.chmod(tmp, file_mode)
    _chown(tmp, target_user)
    os.replace(tmp, path)


def _chown(path: Path, target_user: TargetUser | None) -> None:
    if target_user is None:
        return
    try:
        os.chown(path, target_user.uid, target_user.gid)
    except PermissionError as exc:
        # Survivable: the file still exists, just with the wrong owner.
        # We warn rather than abort so a partial backup doesn't lose
        # content over a chown failure on a single file.
        log.warning(
            "chown %d:%d on %s failed: %s",
            target_user.uid,
            target_user.gid,
            path,
            exc,
        )
