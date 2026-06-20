"""BackupStore contract — one interface, two implementations (FR-2.5).

The store holds *content* and history. Baseline checksums and pending
classification live in ``pending.py``; the store doesn't know which
files are pending, only how to write a copy and read it back.

Two implementations:

  ``store_plain.PlainBackupStore``  Always works. Timestamped copies
                                    under <root>/<entry_id>/<ts>/<path>.
  ``store_git.GitBackupStore``       Same on-disk layout wrapped in a
                                    git repo; each backup operation is
                                    one commit.

Read-side methods (list_snapshots, list_files, read) are stubbed in
this contract so Phase 5 restore can program against the interface
without further changes.

``make_backup_store`` is the FR-2.5 startup-detection factory: it
returns the git-backed store when the ``git`` binary is on PATH and
the plain store otherwise. Callers (CLI, TUI, recovery) use the
factory rather than instantiating a concrete store.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..platform.user import TargetUser

METADATA_FILENAME = "metadata.json"
METADATA_SCHEMA_VERSION = 1

log = logging.getLogger("lazyserver.backup.store")


@dataclass(frozen=True)
class FileMetadata:
    """Per-source metadata captured at snapshot time (Phase 5 restore).

    Lives in a per-snapshot ``metadata.json`` rather than the latest-
    only ``baselines.json`` because spec §7 journey #1 — "restore the
    previous version" — needs the owner/mode of an *older* snapshot,
    and the baseline ledger only keeps the latest. Restore reads this
    to put the file back as ``bind:bind 0640`` rather than relying on
    whatever the current on-disk file (if any) happens to have.

    ``mode`` is permission bits only (``st_mode & 0o7777``); ``sha256``
    is hex of the captured content.
    """

    uid: int
    gid: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class SnapshotRef:
    """A pointer to one stored copy of one source file.

    ``source_path`` is the absolute live-system path the content came
    from (e.g. /etc/named.conf). ``stored_path`` is where the store put
    it. Callers should treat ``stored_path`` as opaque — only the store
    knows the on-disk layout.
    """

    entry_id: str
    timestamp: str
    source_path: Path
    stored_path: Path


class BackupStore(Protocol):
    """Phase 4/5 contract for the backup store."""

    root: Path

    def snapshot(
        self,
        *,
        entry_id: str,
        source: Path,
        timestamp: str,
        metadata: FileMetadata,
    ) -> SnapshotRef:
        """Copy ``source`` into the store under entry_id/timestamp.

        ``timestamp`` is the caller-chosen directory name (one per
        backup *operation*; many sources share it). ``metadata`` is
        captured by the caller from ``stat()`` + content hash; the
        store stashes it for the per-snapshot ``metadata.json`` that
        ``commit_operation`` will write. Raises ``FileExistsError`` if
        the same (entry_id, timestamp, source) combo has already been
        stored — the store never silently overwrites a snapshot.
        """

    def list_snapshots(self, entry_id: str) -> list[str]:
        """Timestamps for ``entry_id``, oldest first. Empty if none."""

    def list_files(self, entry_id: str, timestamp: str) -> list[Path]:
        """Original source paths captured in one snapshot, sorted.

        The reserved ``metadata.json`` is skipped — it is store
        bookkeeping, not a captured source file.
        """

    def read(self, ref: SnapshotRef) -> bytes:
        """Return the stored content for one SnapshotRef."""

    def read_metadata(
        self, entry_id: str, timestamp: str
    ) -> dict[Path, FileMetadata] | None:
        """Per-source metadata for one snapshot; ``None`` if absent.

        Returns ``None`` rather than raising for snapshots written
        before Phase 5's metadata capture: restore falls back to the
        FR-1.8 ownership-resolution chain (sibling → set-dir → YAML
        override → root+warning) in that case, so VM-verified archives
        from Phase 4 keep working without rewriting them.
        """

    def commit_operation(self, *, message: str) -> None:
        """Finalize a backup operation in the store's history.

        Called once per backup operation, after every ``snapshot`` for
        that operation has run and the baseline ledger has been
        persisted. Implementations that have no history layer
        (PlainBackupStore) make this a no-op; the git store stages all
        new/modified paths and records one commit. Idempotent: a
        commit_operation call with nothing to commit must not fail.

        Errors must be logged but not raised: the snapshot content is
        already on disk in the plain layout, so a failed commit is a
        history-only loss, not a data loss. Keeping it non-raising
        means the worker's success/failure reports reflect actual
        snapshot outcomes, not enhancement-layer noise.
        """


def make_backup_store(
    root: Path, target_user: TargetUser | None = None
) -> "BackupStore":
    """Return the right BackupStore for this machine (FR-2.5).

    Decision is taken once at startup based on whether the ``git`` CLI
    is installed. We do not consult ``<root>/.git`` here: an existing
    git repo with no ``git`` binary still has to fall back to plain
    (no way to operate it), and an empty root with ``git`` available
    is the common first-run case (GitBackupStore lazily initialises
    the repo).
    """
    if shutil.which("git"):
        from .store_git import GitBackupStore

        return GitBackupStore(root=root, target_user=target_user)
    from .store_plain import PlainBackupStore

    log.info("git not on PATH: using PlainBackupStore at %s", root)
    return PlainBackupStore(root=root, target_user=target_user)
