"""BackupStore contract — one interface, two implementations (FR-2.5).

The store holds *content* and history. Baseline checksums and pending
classification live in ``pending.py``; the store doesn't know which
files are pending, only how to write a copy and read it back.

Two implementations:

  ``store_plain.PlainBackupStore``  Always works. Timestamped copies
                                    under <root>/<entry_id>/<ts>/<path>.
  ``store_git.GitBackupStore``       Same on-disk layout wrapped in a
                                    git repo; each backup operation is
                                    one commit. Added in 4d.

Read-side methods (list_snapshots, list_files, read) are stubbed in
this contract so Phase 5 restore can program against the interface
without further changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    ) -> SnapshotRef:
        """Copy ``source`` into the store under entry_id/timestamp.

        ``timestamp`` is the caller-chosen directory name (one per
        backup *operation*; many sources share it). Raises
        ``FileExistsError`` if the same (entry_id, timestamp, source)
        combo has already been stored — the store never silently
        overwrites a snapshot.
        """

    def list_snapshots(self, entry_id: str) -> list[str]:
        """Timestamps for ``entry_id``, oldest first. Empty if none."""

    def list_files(self, entry_id: str, timestamp: str) -> list[Path]:
        """Original source paths captured in one snapshot, sorted."""

    def read(self, ref: SnapshotRef) -> bytes:
        """Return the stored content for one SnapshotRef."""
