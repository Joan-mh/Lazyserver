"""Backup orchestration (FR-2.3/2.4): scope selection + snapshot loop.

This is the layer that glues the three other layers together for a
backup operation:

  * ``pending``  decides what's eligible.
  * ``store``    holds the content (Plain in 4b; Git in 4d — orchestration
                 is typed against the BackupStore Protocol so 4d slots in
                 without changes here).
  * baseline ledger persists what was backed up + when + how (sha + orig
                 owner/mode for Phase 5).

Two public entry points cover FR-2.3's three scopes:

  * ``backup_pending(entries=…)``  scope = "all pending" and "all files
                                    of selected entries" — caller passes
                                    either every known entry or a subset.
  * ``backup_files(items=…)``      scope = "individually selected files" —
                                    caller picks the PendingItems.

A single private worker runs the snapshot loop, so both entry points
have identical semantics around timestamps, eligibility, ownership
capture, and partial-failure atomicity.

Atomicity model: snapshots are written one by one; baselines are
updated in memory as each succeeds; the ledger is saved *once* at the
end (atomic via temp+rename in BaselineStore). A failure on item N
leaves items 0..N-1 baselined and the rest unchanged — never half-
baselined. The user sees per-item outcomes in the report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..tconf.resolve import ResolvedEntry
from .checksums import sha256_of
from .pending import (
    Baseline,
    BaselineStore,
    PendingItem,
    PendingStatus,
    scan_all,
)
from .store import BackupStore, SnapshotRef

log = logging.getLogger("lazyserver.backup.run")

TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


class BackupOutcome(str, Enum):
    """Per-item result. Backup itself only produces BACKED_UP or FAILED;
    the SKIPPED_* values report items that were never eligible so the
    caller can render the whole scan, not just the changes."""

    BACKED_UP = "backed_up"
    SKIPPED_UNCHANGED = "skipped_unchanged"
    SKIPPED_MISSING = "skipped_missing"     # baseline yes, on-disk no
    SKIPPED_ABSENT = "skipped_absent"        # never on disk
    FAILED = "failed"


@dataclass(frozen=True)
class BackupReport:
    item: PendingItem
    outcome: BackupOutcome
    ref: SnapshotRef | None = None
    error: str | None = None


# ---------- public API ----------


def backup_pending(
    *,
    entries: Iterable[ResolvedEntry],
    store: BackupStore,
    baselines: BaselineStore,
    timestamp: str | None = None,
) -> list[BackupReport]:
    """Back up everything backup-eligible across ``entries``.

    Pass all known entries for FR-2.3's "all pending" scope; pass a
    subset for the "selected entries" scope. The scan picks up
    file_set members added since the entry was defined (FR-1.6).
    """
    ts = timestamp or current_timestamp()
    items = scan_all(entries, baselines)
    return _execute(items, store=store, baselines=baselines, timestamp=ts)


def backup_files(
    *,
    items: Iterable[PendingItem],
    store: BackupStore,
    baselines: BaselineStore,
    timestamp: str | None = None,
) -> list[BackupReport]:
    """Back up an explicit list of items (FR-2.3 selected-files scope).

    The caller is responsible for having scanned recently enough that
    ``items`` reflects current state; the worker re-checks eligibility
    before touching the store, so a stale UNCHANGED item is reported as
    SKIPPED_UNCHANGED rather than a wasted snapshot.
    """
    ts = timestamp or current_timestamp()
    return _execute(list(items), store=store, baselines=baselines, timestamp=ts)


def current_timestamp() -> str:
    """``YYYYMMDD-HHMMSS`` for *now*. One per backup operation.

    Extracted so tests can patch ``backup.run.current_timestamp``
    instead of mocking the clock — and so the format is one source-
    of-truth for the directory layout shared with the store.
    """
    return datetime.now().strftime(TIMESTAMP_FORMAT)


# ---------- worker ----------


def _execute(
    items: list[PendingItem],
    *,
    store: BackupStore,
    baselines: BaselineStore,
    timestamp: str,
) -> list[BackupReport]:
    reports: list[BackupReport] = []
    pending_baseline_updates: list[tuple[PendingItem, SnapshotRef, str, int, int, int]] = []

    for item in items:
        if item.status is PendingStatus.UNCHANGED:
            reports.append(BackupReport(item, BackupOutcome.SKIPPED_UNCHANGED))
            continue
        if item.status is PendingStatus.MISSING:
            reports.append(BackupReport(item, BackupOutcome.SKIPPED_MISSING))
            continue
        if item.status in (
            PendingStatus.ABSENT_OPTIONAL,
            PendingStatus.ABSENT_REQUIRED,
        ):
            reports.append(BackupReport(item, BackupOutcome.SKIPPED_ABSENT))
            continue
        # NEW or CHANGED — eligible.
        try:
            st = item.path.stat()
            ref = store.snapshot(
                entry_id=item.entry_id,
                source=item.path,
                timestamp=timestamp,
            )
            stored_sha = sha256_of(ref.stored_path)
        except Exception as exc:
            log.warning(
                "backup failed for %s (%s/%s): %s",
                item.path,
                item.entry_id,
                item.file_id,
                exc,
            )
            reports.append(
                BackupReport(item, BackupOutcome.FAILED, error=str(exc))
            )
            continue
        assert stored_sha is not None  # we just wrote it
        pending_baseline_updates.append(
            (item, ref, stored_sha, st.st_uid, st.st_gid, st.st_mode & 0o7777)
        )
        reports.append(BackupReport(item, BackupOutcome.BACKED_UP, ref=ref))

    for item, ref, sha, uid, gid, mode in pending_baseline_updates:
        baselines.set(
            item.entry_id,
            item.path,
            Baseline(
                sha256=sha,
                snapshot=ref.timestamp,
                file_id=item.file_id,
                set_id=item.set_id,
                orig_uid=uid,
                orig_gid=gid,
                orig_mode=mode,
            ),
        )

    if pending_baseline_updates:
        baselines.save()

    return reports
