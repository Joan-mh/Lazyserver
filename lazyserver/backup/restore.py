"""Restore — overwrite live files from chosen snapshots (FR-3, Phase 5).

Two layers, kept apart so the dangerous one is the thin one:

  * **Planning** (pure): pick the snapshot per entry, enumerate the
    files we'd restore, compute the file_set extras we'd report (FR-3.4),
    resolve the per-file ownership source (FR-3.3). Touches the
    BackupStore and the live filesystem for *reads* only — no writes.

  * **Execution** (live): per item, capture a pre-restore snapshot of
    the current on-disk state, then overwrite + chown/chmod.

**Safety contract for execution (FR-3.2 + NFR-2).** The pre-restore
snapshot is what lets restore run *without confirmation*. So the
ordering is enforced: pre-snapshot succeeds → overwrite; pre-snapshot
fails → abort that item, leave the live file untouched. We do not
overwrite a file we couldn't first snapshot. If the live file does not
exist at all, there is nothing to undo and no pre-snapshot is taken.

**Backward compatibility.** Snapshots written before Phase 5 have no
``metadata.json``; ``BackupStore.read_metadata`` returns ``None``. We
fall back to the FR-1.8 ownership-resolution chain — sibling on-disk →
set-dir → file_set YAML override → root+warning — so VM-verified
Phase 4 archives restore correctly without rewriting them.

**Dangerous-mode policy.** Modes are restored literally (snapshot is
authoritative) but a mode that grants the owner no read bit raises a
warning, mirroring the FR-1.8 root:root fallback warning. Silent 0o000
restores would surprise a student; a warning line makes it a lesson.

**No-delete for file_sets (FR-3.4).** Live files matching a set's glob
that are absent from the chosen snapshot are reported as *extras* and
left strictly alone. Mirror/delete is explicitly deferred per spec §5.
The pre-restore snapshot also captures extras, so even an extra
remains recoverable if a future mirror flag is added.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..platform.user import TargetUser
from ..tconf.model import KIND_APP, KIND_SERVICE
from ..tconf.resolve import (
    ResolvedEntry,
    ResolvedFile,
    ResolvedFileSet,
    expand_file_set,
)
from ._fsutil import ensure_owned_dir, write_owned
from .checksums import sha256_of
from .create import plan_ownership
from .pending import Baseline, BaselineStore
from .store import BackupStore, FileMetadata, SnapshotRef

log = logging.getLogger("lazyserver.backup.restore")

PRE_RESTORE_SUFFIX = "-pre-restore"

# Ownership-source labels surfaced in the report (and in tests).
SRC_METADATA = "metadata"
SRC_LIVE_STAT = "live-stat"
SRC_FR18_SIBLING = "sibling"
SRC_FR18_SET_DIR = "set-dir"
SRC_FR18_OVERRIDE = "yaml-override"
SRC_FR18_FALLBACK_ROOT = "fallback-root"
SRC_APP_TARGET_USER = "app-target-user"


# ---------- selection / scope ----------


@dataclass(frozen=True)
class SnapshotChoice:
    """Per-entry timestamp picks; missing entry → latest available."""

    timestamps: dict[str, str] = field(default_factory=dict)

    @classmethod
    def latest_all(cls) -> "SnapshotChoice":
        return cls(timestamps={})

    def pick(self, entry_id: str, available: list[str]) -> str | None:
        """Return the chosen timestamp for ``entry_id``, or None if none.

        ``available`` is the store's ``list_snapshots`` (oldest first).
        Explicit choice wins; otherwise the latest. Explicit choices
        that don't exist resolve to None — caller surfaces it to the
        user rather than silently falling back, so a "restore the
        version from yesterday" intent doesn't quietly become "restore
        whatever the latest is".
        """
        explicit = self.timestamps.get(entry_id)
        if explicit is not None:
            return explicit if explicit in available else None
        return available[-1] if available else None


@dataclass(frozen=True)
class RestoreSelection:
    """What the user asked to restore. Pure value object."""

    entry_ids: tuple[str, ...]
    file_paths: tuple[Path, ...] | None  # None = all files of those entries
    snapshot_choice: SnapshotChoice


# ---------- planner output ----------


@dataclass(frozen=True)
class RestoreItem:
    """One file to be restored — enough info for the worker to execute."""

    entry_id: str
    snapshot: str
    source_path: Path
    ref: SnapshotRef
    file_id: str
    set_id: str | None
    captured_metadata: FileMetadata | None


@dataclass(frozen=True)
class FileSetExtra:
    """A live file_set member that the chosen snapshot does not hold.

    Reported (FR-3.4), never overwritten or deleted. The execution
    layer still takes a pre-restore snapshot of it so a future
    mirror/delete option remains reversible.
    """

    entry_id: str
    set_id: str
    path: Path


@dataclass(frozen=True)
class RestorePlan:
    items: tuple[RestoreItem, ...]
    extras: tuple[FileSetExtra, ...]
    missing_entries: tuple[str, ...]
    """Entries the user asked for that had no snapshots, or whose
    requested timestamp didn't exist — surfaced rather than silently
    skipped."""


# ---------- executor output ----------


class RestoreOutcome(str, Enum):
    RESTORED = "restored"
    PRE_SNAPSHOT_FAILED = "pre_snapshot_failed"  # safety abort, live file untouched
    WRITE_FAILED = "write_failed"  # pre-snapshot taken, write itself failed
    EXTRA_REPORTED = "extra_reported"  # file_set extra (FR-3.4)


@dataclass(frozen=True)
class RestoreReport:
    item: RestoreItem | None
    extra: FileSetExtra | None
    outcome: RestoreOutcome
    pre_snapshot_ref: SnapshotRef | None = None
    chosen_uid: int | None = None
    chosen_gid: int | None = None
    chosen_mode: int | None = None
    ownership_source: str | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None


# ---------- planning (pure-ish) ----------


def plan_restore(
    *,
    selection: RestoreSelection,
    resolved_entries: dict[str, ResolvedEntry],
    store: BackupStore,
) -> RestorePlan:
    """Compute the restore plan without touching the live filesystem.

    For each requested entry: pick a snapshot (per the choice), build
    one RestoreItem per file the snapshot holds, and compute the
    file_set extras by globbing the live disk and subtracting the
    snapshot set.

    Reads:
      * store.list_snapshots / list_files / read_metadata
      * the live filesystem, but only via ``expand_file_set`` to
        compute extras (no writes, no stat of fixed-file paths).
    """
    items: list[RestoreItem] = []
    extras: list[FileSetExtra] = []
    missing: list[str] = []

    selected_paths = (
        set(selection.file_paths) if selection.file_paths is not None else None
    )

    for entry_id in selection.entry_ids:
        if entry_id not in resolved_entries:
            missing.append(entry_id)
            continue
        available = store.list_snapshots(entry_id)
        ts = selection.snapshot_choice.pick(entry_id, available)
        if ts is None:
            missing.append(entry_id)
            continue

        snap_files = store.list_files(entry_id, ts)
        metadata_map = store.read_metadata(entry_id, ts) or {}

        # Decide each snapshot file's file_id/set_id from the entry definition.
        resolved = resolved_entries[entry_id]
        fixed_by_path = {Path(rf.path): rf for rf in resolved.files}
        set_dirs: list[tuple[ResolvedFileSet, Path]] = [
            (fs, Path(fs.directory)) for fs in resolved.file_sets
        ]

        snap_paths_set = set(snap_files)
        for path in snap_files:
            if selected_paths is not None and path not in selected_paths:
                continue
            file_id, set_id = _classify_snapshot_path(path, fixed_by_path, set_dirs)
            ref = _make_ref(store, entry_id=entry_id, ts=ts, source_path=path)
            items.append(
                RestoreItem(
                    entry_id=entry_id,
                    snapshot=ts,
                    source_path=path,
                    ref=ref,
                    file_id=file_id,
                    set_id=set_id,
                    captured_metadata=metadata_map.get(path),
                )
            )

        # File_set extras: live members not in the snapshot.
        for fs in resolved.file_sets:
            on_disk = set(expand_file_set(fs))
            for p in sorted(on_disk - snap_paths_set):
                if selected_paths is not None and p not in selected_paths:
                    continue
                extras.append(
                    FileSetExtra(entry_id=entry_id, set_id=fs.id, path=p)
                )

    return RestorePlan(
        items=tuple(items),
        extras=tuple(extras),
        missing_entries=tuple(missing),
    )


# ---------- ownership (pure) ----------


@dataclass(frozen=True)
class OwnershipChoice:
    uid: int
    gid: int
    mode: int  # permission bits & 0o7777
    source: str  # one of SRC_* labels
    warnings: tuple[str, ...] = ()


def resolve_ownership(
    *,
    captured: FileMetadata | None,
    live_path: Path,
    entry_kind: str,
    target_user: TargetUser,
    file_set: ResolvedFileSet | None,
) -> OwnershipChoice:
    """FR-3.3 ownership resolution for a restore (pure).

    Order:
      1. Captured metadata from the snapshot — authoritative.
      2. Live on-disk file's stat (preserves whatever owner the file has now).
      3. FR-1.8 chain via ``plan_ownership`` (sibling / set-dir / override / root).

    Mode is treated as authoritative when captured, but a warning is
    attached if the mode would leave the owner without read access
    (e.g. 0o000) — see ``looks_dangerous``. For app entries we always
    return the target user, mirroring FR-1.8.
    """
    if captured is not None:
        warnings = (
            (_dangerous_mode_warning(captured.mode),)
            if looks_dangerous(captured.mode)
            else ()
        )
        return OwnershipChoice(
            uid=captured.uid,
            gid=captured.gid,
            mode=captured.mode,
            source=SRC_METADATA,
            warnings=warnings,
        )

    # No captured metadata — backward-compat path for pre-Phase-5 snapshots.
    if entry_kind == KIND_APP:
        return OwnershipChoice(
            uid=target_user.uid,
            gid=target_user.gid,
            mode=0o644,
            source=SRC_APP_TARGET_USER,
        )

    # Service entry, no metadata: prefer the live file's stat if present.
    if live_path.exists():
        st = live_path.stat()
        mode = st.st_mode & 0o7777
        warnings = (
            (_dangerous_mode_warning(mode),) if looks_dangerous(mode) else ()
        )
        return OwnershipChoice(
            uid=st.st_uid,
            gid=st.st_gid,
            mode=mode,
            source=SRC_LIVE_STAT,
            warnings=warnings,
        )

    # Last resort: FR-1.8 chain. Drives off the file_set if any (sibling/dir/override).
    plan = plan_ownership(
        entry_kind=KIND_SERVICE,
        directory=Path(file_set.directory) if file_set else live_path.parent,
        target_user=target_user,
        explicit_owner=file_set.owner if file_set else None,
        explicit_group=file_set.group if file_set else None,
        explicit_mode=file_set.mode if file_set else None,
    )
    uid, gid = _names_to_ids(plan.owner, plan.group)
    mode = int(plan.mode, 8)
    if plan.is_fallback_root:
        source = SRC_FR18_FALLBACK_ROOT
        warnings: tuple[str, ...] = (
            "ownership fallback to root:root — the service may be unable to read this file",
        )
    elif file_set and (file_set.owner or file_set.group or file_set.mode):
        source = SRC_FR18_OVERRIDE
        warnings = ()
    elif "sibling" in plan.reason:
        source = SRC_FR18_SIBLING
        warnings = ()
    else:
        source = SRC_FR18_SET_DIR
        warnings = ()
    if looks_dangerous(mode):
        warnings = (*warnings, _dangerous_mode_warning(mode))
    return OwnershipChoice(uid=uid, gid=gid, mode=mode, source=source, warnings=warnings)


def looks_dangerous(mode: int) -> bool:
    """True when ``mode`` grants the owner no read access.

    A literal restore is correct (snapshot is authoritative) but a 0o000
    or 0o100 leaves the file unreadable by its own owner — almost
    certainly a config error the student needs to know about, not
    something they meant. Same posture as the FR-1.8 root:root warning.
    """
    return (mode & 0o400) == 0


def _dangerous_mode_warning(mode: int) -> str:
    return (
        f"restored mode {oct(mode)} grants the owner no read access — "
        f"the file may be unreadable to its service or owner"
    )


# ---------- execution (live writes) ----------


def execute_restore(
    plan: RestorePlan,
    *,
    store: BackupStore,
    baselines: BaselineStore,
    resolved_entries: dict[str, ResolvedEntry],
    target_user: TargetUser,
    pre_restore_timestamp: str,
) -> list[RestoreReport]:
    """Run the plan with the FR-3.2 safety contract.

    For each item:
      1. If the live file exists, take a pre-restore snapshot.
         Failure → record PRE_SNAPSHOT_FAILED, **do not overwrite**.
      2. Resolve ownership (FR-3.3); read snapshot bytes; write atomically
         via ``write_owned``, then chown/chmod to the resolved owner.
         Failure here → record WRITE_FAILED; the pre-restore snapshot
         is still in the store so the previous state remains recoverable.
      3. Update the baseline ledger to the restored content's sha so
         the file isn't flagged pending after restore.

    Each ``FileSetExtra`` becomes one EXTRA_REPORTED report row,
    consumed by callers for the post-restore summary (FR-3.4).
    """
    reports: list[RestoreReport] = []
    pre_restore_ts = f"{pre_restore_timestamp}{PRE_RESTORE_SUFFIX}"

    pending_baseline_updates: list[tuple[RestoreItem, OwnershipChoice]] = []

    for item in plan.items:
        # ---- 1. pre-restore snapshot (safety gate) ----
        pre_ref: SnapshotRef | None = None
        if item.source_path.exists():
            try:
                pre_ref = _take_pre_snapshot(
                    store=store,
                    item=item,
                    pre_restore_ts=pre_restore_ts,
                )
            except Exception as exc:
                log.warning(
                    "pre-restore snapshot failed for %s (%s): %s — "
                    "live file NOT overwritten",
                    item.source_path, item.entry_id, exc,
                )
                reports.append(
                    RestoreReport(
                        item=item,
                        extra=None,
                        outcome=RestoreOutcome.PRE_SNAPSHOT_FAILED,
                        error=str(exc),
                    )
                )
                continue

        # ---- 2. ownership + write ----
        resolved = resolved_entries.get(item.entry_id)
        entry_kind = resolved.entry.kind if resolved else KIND_SERVICE
        file_set = _matching_file_set(resolved, item.set_id) if resolved else None
        ownership = resolve_ownership(
            captured=item.captured_metadata,
            live_path=item.source_path,
            entry_kind=entry_kind,
            target_user=target_user,
            file_set=file_set,
        )
        try:
            _write_restored_file(
                item=item,
                ownership=ownership,
                store=store,
                target_user=target_user,
                entry_kind=entry_kind,
            )
        except Exception as exc:
            log.warning(
                "restore write failed for %s (%s): %s — pre-restore "
                "snapshot in %s remains intact",
                item.source_path, item.entry_id, exc, pre_restore_ts,
            )
            reports.append(
                RestoreReport(
                    item=item,
                    extra=None,
                    outcome=RestoreOutcome.WRITE_FAILED,
                    pre_snapshot_ref=pre_ref,
                    chosen_uid=ownership.uid,
                    chosen_gid=ownership.gid,
                    chosen_mode=ownership.mode,
                    ownership_source=ownership.source,
                    warnings=ownership.warnings,
                    error=str(exc),
                )
            )
            continue

        pending_baseline_updates.append((item, ownership))
        reports.append(
            RestoreReport(
                item=item,
                extra=None,
                outcome=RestoreOutcome.RESTORED,
                pre_snapshot_ref=pre_ref,
                chosen_uid=ownership.uid,
                chosen_gid=ownership.gid,
                chosen_mode=ownership.mode,
                ownership_source=ownership.source,
                warnings=ownership.warnings,
            )
        )

    # ---- 3. baseline updates + commit ----
    for item, ownership in pending_baseline_updates:
        sha = item.captured_metadata.sha256 if item.captured_metadata else (
            sha256_of(item.source_path) or ""
        )
        baselines.set(
            item.entry_id,
            item.source_path,
            Baseline(
                sha256=sha,
                snapshot=item.snapshot,
                file_id=item.file_id,
                set_id=item.set_id,
                orig_uid=ownership.uid,
                orig_gid=ownership.gid,
                orig_mode=ownership.mode,
            ),
        )

    if pending_baseline_updates:
        baselines.save()
        store.commit_operation(message=f"restore {pre_restore_timestamp}")

    # FR-3.4 extras get one report row each (reported, not touched).
    for extra in plan.extras:
        reports.append(
            RestoreReport(
                item=None,
                extra=extra,
                outcome=RestoreOutcome.EXTRA_REPORTED,
            )
        )

    return reports


# ---------- internal helpers ----------


def _take_pre_snapshot(
    *,
    store: BackupStore,
    item: RestoreItem,
    pre_restore_ts: str,
) -> SnapshotRef:
    """Capture the current on-disk file as a pre-restore snapshot.

    Reads stat + sha *of the live file* — this is the version we're
    about to overwrite, so this is exactly the bytes/metadata we need
    to preserve.
    """
    st = item.source_path.stat()
    sha = sha256_of(item.source_path)
    if sha is None:  # belt and braces; we just stat'd it
        raise OSError(f"could not hash live file {item.source_path}")
    metadata = FileMetadata(
        uid=st.st_uid,
        gid=st.st_gid,
        mode=st.st_mode & 0o7777,
        sha256=sha,
    )
    return store.snapshot(
        entry_id=item.entry_id,
        source=item.source_path,
        timestamp=pre_restore_ts,
        metadata=metadata,
    )


def _write_restored_file(
    *,
    item: RestoreItem,
    ownership: OwnershipChoice,
    store: BackupStore,
    target_user: TargetUser,
    entry_kind: str,
) -> None:
    """Overwrite the live file with stored content; apply resolved owner/mode.

    Uses ``write_owned`` for the atomic write (tmp + rename), then
    re-chowns/chmods to the resolved (uid, gid, mode). For app entries
    we also ensure_owned_dir on the parent so a previously-missing
    ``~/.config/<app>/`` dir lands target-user-owned, not root-owned.
    """
    if entry_kind == KIND_APP:
        ensure_owned_dir(item.source_path.parent, target_user)
    else:
        item.source_path.parent.mkdir(parents=True, exist_ok=True)
    content = store.read(item.ref)
    write_owned(item.source_path, content, target_user)
    # Apply the resolved ownership and mode. PermissionError here is
    # surfaced (we ran as root per NFR-3); a KeyError on uid/gid would
    # already have come out of resolve_ownership.
    os.chown(item.source_path, ownership.uid, ownership.gid)
    os.chmod(item.source_path, ownership.mode)


def _matching_file_set(
    resolved: ResolvedEntry, set_id: str | None
) -> ResolvedFileSet | None:
    if set_id is None:
        return None
    for fs in resolved.file_sets:
        if fs.id == set_id:
            return fs
    return None


def _classify_snapshot_path(
    path: Path,
    fixed_by_path: dict[Path, ResolvedFile],
    set_dirs: list[tuple[ResolvedFileSet, Path]],
) -> tuple[str, str | None]:
    """Decide a stored file's (file_id, set_id) from the entry definition.

    A fixed-file path match wins; otherwise we attribute the file to the
    first file_set whose directory is an ancestor (longest-prefix order
    so nested set dirs disambiguate). Unrecognised paths get the
    file_id ``"unknown"`` and ``set_id=None`` — the file can still be
    restored, but the baseline update labels it as legacy.
    """
    fixed = fixed_by_path.get(path)
    if fixed is not None:
        return fixed.id, None
    # Longest dir prefix first so /etc/bind/sub/* matches the sub set
    # before /etc/bind/* would.
    for fs, directory in sorted(set_dirs, key=lambda kv: len(str(kv[1])), reverse=True):
        try:
            path.relative_to(directory)
        except ValueError:
            continue
        return fs.id, fs.id
    return "unknown", None


def _make_ref(
    store: BackupStore, *, entry_id: str, ts: str, source_path: Path
) -> SnapshotRef:
    """Construct the SnapshotRef the store would produce for this path.

    The store doesn't expose a "give me the ref for an already-stored
    file" method — its public Protocol is write-then-read. We build the
    ref directly using the same path arithmetic the Plain store uses.
    """
    stored_path = store.root / entry_id / ts / Path(*source_path.parts[1:])
    return SnapshotRef(
        entry_id=entry_id,
        timestamp=ts,
        source_path=source_path,
        stored_path=stored_path,
    )


def _names_to_ids(owner: str, group: str) -> tuple[int, int]:
    import grp
    import pwd
    return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid
