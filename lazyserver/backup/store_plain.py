"""PlainBackupStore — timestamped copies, always works (FR-2.5).

The fallback when git is unavailable, and the always-correct baseline
implementation that the git store is checked against. Layout::

    <root>/
    └── <entry_id>/
        └── <YYYYMMDD-HHMMSS>/        # one per backup operation
            └── <source path without leading />
                e.g. etc/named.conf

The store doesn't track checksums (that's the ledger's job) or decide
which files are pending (that's the scanner's job). It writes content
into a layout that's trivially browsable, and reads it back. Every
directory and file written lands as ``target_user:target_group`` with
mode ``0755``/``0644`` so the student can rsync, git-clone, or chown
their archive without root.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..platform.user import TargetUser
from ._fsutil import mkdir_owned_chain, write_owned
from .store import SnapshotRef

log = logging.getLogger("lazyserver.backup.store_plain")


@dataclass
class PlainBackupStore:
    """One BackupStore implementation. See ``store.BackupStore``."""

    root: Path
    target_user: TargetUser | None = None

    def snapshot(
        self,
        *,
        entry_id: str,
        source: Path,
        timestamp: str,
    ) -> SnapshotRef:
        if not source.is_absolute():
            raise ValueError(f"snapshot source must be absolute: {source}")
        stored_path = self._stored_path(entry_id, timestamp, source)
        if stored_path.exists():
            raise FileExistsError(
                f"snapshot already exists: {entry_id}/{timestamp} "
                f"already holds a copy of {source}"
            )
        mkdir_owned_chain(
            stored_path.parent, self.target_user, stop_at=self.root
        )
        write_owned(stored_path, source.read_bytes(), self.target_user)
        return SnapshotRef(
            entry_id=entry_id,
            timestamp=timestamp,
            source_path=source,
            stored_path=stored_path,
        )

    def list_snapshots(self, entry_id: str) -> list[str]:
        entry_dir = self.root / entry_id
        if not entry_dir.is_dir():
            return []
        return sorted(p.name for p in entry_dir.iterdir() if p.is_dir())

    def list_files(self, entry_id: str, timestamp: str) -> list[Path]:
        snap_dir = self.root / entry_id / timestamp
        if not snap_dir.is_dir():
            return []
        sources: list[Path] = []
        for p in snap_dir.rglob("*"):
            if p.is_file():
                relative = p.relative_to(snap_dir)
                sources.append(Path("/") / relative)
        return sorted(sources)

    def read(self, ref: SnapshotRef) -> bytes:
        return ref.stored_path.read_bytes()

    def commit_operation(self, *, message: str) -> None:
        # No history layer; nothing to finalize. See BackupStore Protocol.
        return None

    # ---------- internal ----------

    def _stored_path(self, entry_id: str, timestamp: str, source: Path) -> Path:
        # Strip the leading '/' so we can join under the snapshot dir.
        # Symmetric with list_files's re-prepend so the round-trip
        # source → stored → source holds.
        relative = Path(*source.parts[1:])
        return self.root / entry_id / timestamp / relative
