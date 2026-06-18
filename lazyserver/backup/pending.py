"""Baseline ledger + pending scan (FR-2.1, FR-2.2).

Pending is *derived*, not stored. We persist a baseline ledger of
{(entry_id, absolute_path) → sha256 at last backup} and, on each scan,
compare it against the current filesystem. Anything that differs is
pending. This rules out the class of bug where a "pending" set drifts
out of sync with the actual file contents.

The ledger lives at ``<backup_store>/baselines.json``. Writes are atomic
(temp + rename) so a crash mid-backup cannot corrupt the file. A scan
walks three sources:

1. The entry's fixed `files` — known ids, fixed paths.
2. The entry's `file_sets` — glob expanded at scan time (FR-1.6), so
   files created after the entry was defined are caught immediately.
3. Baselined paths for the same entry that *aren't* covered by (1) or
   (2): a file that was backed up earlier but has since been deleted.
   These surface as MISSING — we don't silently forget them.

Six possible per-path statuses:

  NEW              disk yes, baseline no → backup-eligible
  CHANGED          both yes, sha differs → backup-eligible
  UNCHANGED        both yes, sha same → not pending
  MISSING          disk no,  baseline yes → surfaced, *not* backed up
  ABSENT_OPTIONAL  declared optional, never on disk → silent
  ABSENT_REQUIRED  declared required, never on disk → surfaced as warning

A status is "pending" when the user should see it in the pending list:
NEW, CHANGED, MISSING, or ABSENT_REQUIRED. Only NEW and CHANGED are
backup-eligible — MISSING has no content to capture (Phase 5 restore
covers it), and ABSENT_REQUIRED is a configuration warning, not a
backup action.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from ..platform.user import TargetUser
from ..tconf.resolve import ResolvedEntry, ResolvedFile, ResolvedFileSet, expand_file_set
from ._fsutil import mkdir_owned_chain, write_owned
from .checksums import sha256_of

log = logging.getLogger("lazyserver.backup.pending")

BASELINES_FILENAME = "baselines.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Baseline:
    """One ledger row: the sha at last backup + provenance."""

    sha256: str
    snapshot: str          # timestamp dir, e.g. "20260618-103045"
    file_id: str           # the originating tconf id (file or set)
    set_id: str | None     # None for fixed files; == file_id for set members


class BaselineStore:
    """Persistent ledger keyed by (entry_id, absolute path).

    ``root=None`` keeps the ledger in memory only — useful for the scan
    tests and for any session running without a configured backup store.
    ``target_user`` is the FR-1.10 target user; when set, the ledger
    file (and any directories we create above it) land target-user-
    owned so a root-run session doesn't leave a root-owned archive
    behind. The configured ``root`` is never chowned — that ownership
    is the user's pre-existing choice.
    """

    def __init__(
        self,
        root: Path | None,
        target_user: TargetUser | None = None,
    ) -> None:
        self.root = root
        self.target_user = target_user
        self._entries: dict[str, dict[str, dict]] = {}

    # ---------- load / save ----------

    @classmethod
    def load(
        cls,
        root: Path | None,
        target_user: TargetUser | None = None,
    ) -> "BaselineStore":
        inst = cls(root, target_user=target_user)
        if root is None:
            return inst
        path = root / BASELINES_FILENAME
        if not path.exists():
            return inst
        with path.open() as fp:
            raw = json.load(fp)
        version = raw.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported baseline schema version {version!r} at {path} "
                f"(expected {SCHEMA_VERSION})."
            )
        inst._entries = dict(raw.get("entries", {}))
        return inst

    def save(self) -> None:
        """Write the ledger atomically with the right ownership.

        Atomic via temp+rename so a crash mid-write never corrupts the
        ledger. No-op when root is None.
        """
        if self.root is None:
            return
        # Ensure the root exists; if it predates us, mkdir_owned_chain
        # is a no-op for it (boundary respected).
        self.root.mkdir(parents=True, exist_ok=True)
        mkdir_owned_chain(self.root, self.target_user, stop_at=self.root)
        target = self.root / BASELINES_FILENAME
        payload = {"version": SCHEMA_VERSION, "entries": self._entries}
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        write_owned(target, body, self.target_user)

    # ---------- get / set / iterate ----------

    def get(self, entry_id: str, path: Path) -> Baseline | None:
        files = self._entries.get(entry_id, {}).get("files", {})
        rec = files.get(str(path))
        if rec is None:
            return None
        return _baseline_from_record(rec)

    def set(self, entry_id: str, path: Path, baseline: Baseline) -> None:
        bucket = self._entries.setdefault(entry_id, {"files": {}})
        bucket["files"][str(path)] = {
            "sha256": baseline.sha256,
            "snapshot": baseline.snapshot,
            "file_id": baseline.file_id,
            "set_id": baseline.set_id,
        }

    def iter_entry(self, entry_id: str) -> Iterator[tuple[Path, Baseline]]:
        files = self._entries.get(entry_id, {}).get("files", {})
        for path_str, rec in files.items():
            yield Path(path_str), _baseline_from_record(rec)


def _baseline_from_record(rec: dict) -> Baseline:
    return Baseline(
        sha256=rec["sha256"],
        snapshot=rec["snapshot"],
        file_id=rec["file_id"],
        set_id=rec.get("set_id"),
    )


# ---------- scan ----------


class PendingStatus(str, Enum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    MISSING = "missing"
    ABSENT_OPTIONAL = "absent_optional"
    ABSENT_REQUIRED = "absent_required"

    def is_backup_eligible(self) -> bool:
        return self in (PendingStatus.NEW, PendingStatus.CHANGED)

    def is_pending(self) -> bool:
        return self in (
            PendingStatus.NEW,
            PendingStatus.CHANGED,
            PendingStatus.MISSING,
            PendingStatus.ABSENT_REQUIRED,
        )


@dataclass(frozen=True)
class PendingItem:
    entry_id: str
    file_id: str
    set_id: str | None
    path: Path
    status: PendingStatus
    current_sha: str | None
    baseline_sha: str | None


def scan_entry(entry: ResolvedEntry, baselines: BaselineStore) -> list[PendingItem]:
    """Return one PendingItem per (live or baselined) path for `entry`.

    Sorted by absolute path. Visits fixed files, then expanded file_set
    members, then any baselined paths that didn't appear in either —
    those are MISSING (deleted-since-backup), surfaced explicitly.
    """
    entry_id = entry.entry.id
    seen: dict[Path, PendingItem] = {}

    for rf in entry.files:
        path = Path(rf.path)
        seen[path] = _classify_fixed(entry_id, rf, path, baselines)

    for fs in entry.file_sets:
        for member in expand_file_set(fs):
            seen[member] = _classify_set_member(entry_id, fs, member, baselines)

    for path, baseline in baselines.iter_entry(entry_id):
        if path in seen:
            continue
        seen[path] = PendingItem(
            entry_id=entry_id,
            file_id=baseline.file_id,
            set_id=baseline.set_id,
            path=path,
            status=PendingStatus.MISSING,
            current_sha=None,
            baseline_sha=baseline.sha256,
        )

    return sorted(seen.values(), key=lambda i: str(i.path))


def scan_all(
    entries: Iterable[ResolvedEntry], baselines: BaselineStore
) -> list[PendingItem]:
    """Scan every entry; concatenate. Order: entry id, then path."""
    out: list[PendingItem] = []
    for entry in sorted(entries, key=lambda e: e.entry.id):
        out.extend(scan_entry(entry, baselines))
    return out


def pending_only(items: Iterable[PendingItem]) -> list[PendingItem]:
    """Filter a scan to entries the user should act on or see."""
    return [i for i in items if i.status.is_pending()]


# ---------- classification ----------


def _classify_fixed(
    entry_id: str,
    rf: ResolvedFile,
    path: Path,
    baselines: BaselineStore,
) -> PendingItem:
    current = sha256_of(path) if path.exists() else None
    baseline = baselines.get(entry_id, path)
    status = _status(current, baseline, optional=rf.optional)
    return PendingItem(
        entry_id=entry_id,
        file_id=rf.id,
        set_id=None,
        path=path,
        status=status,
        current_sha=current,
        baseline_sha=baseline.sha256 if baseline else None,
    )


def _classify_set_member(
    entry_id: str,
    fs: ResolvedFileSet,
    path: Path,
    baselines: BaselineStore,
) -> PendingItem:
    # expand_file_set returns existing files only, so current is never None.
    current = sha256_of(path)
    baseline = baselines.get(entry_id, path)
    if baseline is None:
        status = PendingStatus.NEW
    elif current == baseline.sha256:
        status = PendingStatus.UNCHANGED
    else:
        status = PendingStatus.CHANGED
    return PendingItem(
        entry_id=entry_id,
        file_id=fs.id,
        set_id=fs.id,
        path=path,
        status=status,
        current_sha=current,
        baseline_sha=baseline.sha256 if baseline else None,
    )


def _status(
    current: str | None, baseline: Baseline | None, *, optional: bool
) -> PendingStatus:
    if current is None and baseline is None:
        return (
            PendingStatus.ABSENT_OPTIONAL
            if optional
            else PendingStatus.ABSENT_REQUIRED
        )
    if current is None:
        return PendingStatus.MISSING
    if baseline is None:
        return PendingStatus.NEW
    if current == baseline.sha256:
        return PendingStatus.UNCHANGED
    return PendingStatus.CHANGED
