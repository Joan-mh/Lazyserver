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

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..platform.user import TargetUser
from ._fsutil import mkdir_owned_chain, write_owned
from .store import (
    METADATA_FILENAME,
    METADATA_SCHEMA_VERSION,
    FileMetadata,
    SnapshotRef,
)

log = logging.getLogger("lazyserver.backup.store_plain")


@dataclass
class PlainBackupStore:
    """One BackupStore implementation. See ``store.BackupStore``."""

    root: Path
    target_user: TargetUser | None = None
    _pending_meta: dict[tuple[str, str], dict[Path, FileMetadata]] = field(
        default_factory=dict, init=False, repr=False,
    )

    def snapshot(
        self,
        *,
        entry_id: str,
        source: Path,
        timestamp: str,
        metadata: FileMetadata,
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
        self._pending_meta.setdefault((entry_id, timestamp), {})[source] = metadata
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
            if not p.is_file():
                continue
            # metadata.json at the snapshot root is bookkeeping, not a
            # captured source — skip it so callers see only real files.
            if p.parent == snap_dir and p.name == METADATA_FILENAME:
                continue
            relative = p.relative_to(snap_dir)
            sources.append(Path("/") / relative)
        return sorted(sources)

    def read(self, ref: SnapshotRef) -> bytes:
        return ref.stored_path.read_bytes()

    def read_metadata(
        self, entry_id: str, timestamp: str
    ) -> dict[Path, FileMetadata] | None:
        path = self.root / entry_id / timestamp / METADATA_FILENAME
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as fp:
            raw = json.load(fp)
        version = raw.get("schema_version")
        if version != METADATA_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported metadata schema version {version!r} at {path} "
                f"(expected {METADATA_SCHEMA_VERSION})."
            )
        files = raw.get("files", {})
        return {
            Path(src): FileMetadata(
                uid=rec["uid"],
                gid=rec["gid"],
                mode=rec["mode"],
                sha256=rec["sha256"],
            )
            for src, rec in files.items()
        }

    def commit_operation(self, *, message: str) -> None:
        """Write per-snapshot ``metadata.json`` for each operation key.

        One file per (entry_id, timestamp): a backup operation can span
        multiple entries but each writes into its own snapshot dir, so
        the metadata follows the same partition. Cleared after writing
        so a long-lived store instance (TUI session) doesn't accumulate
        stale entries.
        """
        for (entry_id, ts), files_meta in self._pending_meta.items():
            self._write_metadata(entry_id, ts, files_meta)
        self._pending_meta.clear()
        return None

    # ---------- internal ----------

    def _stored_path(self, entry_id: str, timestamp: str, source: Path) -> Path:
        # Strip the leading '/' so we can join under the snapshot dir.
        # Symmetric with list_files's re-prepend so the round-trip
        # source → stored → source holds.
        relative = Path(*source.parts[1:])
        return self.root / entry_id / timestamp / relative

    def _write_metadata(
        self,
        entry_id: str,
        timestamp: str,
        files_meta: dict[Path, FileMetadata],
    ) -> None:
        path = self.root / entry_id / timestamp / METADATA_FILENAME
        payload = {
            "schema_version": METADATA_SCHEMA_VERSION,
            "files": {
                str(src): {
                    "uid": m.uid,
                    "gid": m.gid,
                    "mode": m.mode,
                    "sha256": m.sha256,
                }
                for src, m in sorted(files_meta.items(), key=lambda kv: str(kv[0]))
            },
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        write_owned(path, body, self.target_user)
