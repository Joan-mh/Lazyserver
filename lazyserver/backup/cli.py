"""Backup CLI dispatcher (FR-2.3/2.4) — reused by Phase 6 recovery.

Three modes, mutually exclusive:

  ``--list``               read-only: show what's pending without writing.
  ``--all``                back up every pending file across every entry.
  ``--entry ID [ID...]``   back up these entries' pending files only.

All three respect the global ``--dry-run``: under dry-run, no snapshots
are written and the baseline ledger is never persisted; the output is
the would-be plan rather than a report of writes.

Exits 0 on success, 1 on hard error (no store configured, unknown
entry id, bootstrap failure), 2 on partial failure (some snapshots
failed). Recovery in Phase 6 reads these codes to decide whether to
proceed with the next phase.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..app import AppContext, BootstrapError, bootstrap
from ..config import Settings, expand_user_path, resolved_backup_store
from ..platform.user import TargetUser
from ..tconf.resolve import ResolutionError, ResolvedEntry, resolve
from .pending import (
    BaselineStore,
    BASELINES_FILENAME,
    PendingItem,
    PendingStatus,
    pending_only,
    scan_all,
)
from .run import (
    BackupOutcome,
    BackupReport,
    backup_pending,
    current_timestamp,
)
from .store import make_backup_store

log = logging.getLogger("lazyserver.backup.cli")

EXIT_OK = 0
EXIT_HARD_ERROR = 1
EXIT_PARTIAL_FAILURE = 2


# ---------- entry point ----------


def cmd_backup(
    *,
    list_only: bool,
    all_pending: bool,
    entry_ids: list[str] | None,
    store_override: Path | None,
    dry_run: bool,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Argparse-friendly entry point. The CLI module dispatches here."""
    try:
        context = bootstrap()
    except BootstrapError as exc:
        print(f"lazyserver: {exc}", file=err)
        return EXIT_HARD_ERROR

    try:
        store_path = _resolve_store_path(
            context.settings, store_override, context.target_user
        )
    except _ConfigError as exc:
        print(f"lazyserver: {exc}", file=err)
        return EXIT_HARD_ERROR

    resolved, unknown = _select_entries(context, entry_ids)
    if unknown:
        print(
            f"lazyserver: unknown entry id(s): {', '.join(sorted(unknown))}",
            file=err,
        )
        return EXIT_HARD_ERROR

    if list_only or dry_run:
        _print_plan(resolved, store_path, dry_run=dry_run, out=out)
        return EXIT_OK

    return _run_real(resolved, store_path, context, out=out, err=err)


# ---------- modes ----------


def _print_plan(
    entries: list[ResolvedEntry],
    store_path: Path,
    *,
    dry_run: bool,
    out,
) -> None:
    """Scan + print without writing. Shared by --list and --dry-run."""
    baselines = BaselineStore.load(store_path)
    items = scan_all(entries, baselines)
    if not items:
        print("(no managed files to scan — no entries matched)", file=out)
        return

    by_entry: dict[str, list[PendingItem]] = {}
    for item in items:
        by_entry.setdefault(item.entry_id, []).append(item)

    eligible_count = 0
    for entry_id in sorted(by_entry):
        rows = by_entry[entry_id]
        print(f"{entry_id} ({len(rows)} file{'s' if len(rows) != 1 else ''}):", file=out)
        for item in rows:
            tag = item.status.value.upper().ljust(16)
            print(f"  {tag} {item.path}", file=out)
            if item.status.is_backup_eligible():
                eligible_count += 1

    print(file=out)
    if dry_run:
        verb = "would be backed up"
    else:
        verb = "pending"
    print(
        f"{eligible_count} file(s) {verb}. Store: {store_path}",
        file=out,
    )


def _run_real(
    entries: list[ResolvedEntry],
    store_path: Path,
    context: AppContext,
    *,
    out,
    err,
) -> int:
    """Actually write snapshots + commit."""
    store_path.mkdir(parents=True, exist_ok=True)
    baselines = BaselineStore.load(store_path, target_user=context.target_user)
    store = make_backup_store(store_path, target_user=context.target_user)
    timestamp = current_timestamp()
    reports = backup_pending(
        entries=entries, store=store, baselines=baselines, timestamp=timestamp,
    )
    return _print_report(reports, store_path, timestamp, out=out, err=err)


def _print_report(
    reports: list[BackupReport],
    store_path: Path,
    timestamp: str,
    *,
    out,
    err,
) -> int:
    by_entry: dict[str, list[BackupReport]] = {}
    for rep in reports:
        by_entry.setdefault(rep.item.entry_id, []).append(rep)

    backed_up = failed = 0
    for entry_id in sorted(by_entry):
        rows = by_entry[entry_id]
        print(f"{entry_id}:", file=out)
        for rep in rows:
            icon, note = _outcome_glyph(rep.outcome, rep.error)
            print(f"  {icon} {rep.item.path}    {note}", file=out)
            if rep.outcome is BackupOutcome.BACKED_UP:
                backed_up += 1
            elif rep.outcome is BackupOutcome.FAILED:
                failed += 1

    print(file=out)
    print(
        f"Backed up {backed_up} file(s) at {timestamp}. Store: {store_path}",
        file=out,
    )
    if failed:
        print(f"⚠ {failed} file(s) failed — see lines marked ✗ above.", file=err)
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def _outcome_glyph(outcome: BackupOutcome, error: str | None) -> tuple[str, str]:
    if outcome is BackupOutcome.BACKED_UP:
        return "✓", "backed up"
    if outcome is BackupOutcome.SKIPPED_UNCHANGED:
        return "⊘", "unchanged"
    if outcome is BackupOutcome.SKIPPED_MISSING:
        return "!", "baselined but missing on disk"
    if outcome is BackupOutcome.SKIPPED_ABSENT:
        return "·", "not present"
    if outcome is BackupOutcome.FAILED:
        return "✗", f"failed: {error or 'unknown'}"
    return "?", outcome.value


# ---------- helpers ----------


class _ConfigError(RuntimeError):
    """Raised when config prevents us from running (e.g. no store)."""


def _resolve_store_path(
    settings: Settings, override: Path | None, target_user: TargetUser
) -> Path:
    if override is not None:
        return expand_user_path(str(override), target_user)
    resolved = resolved_backup_store(settings, target_user)
    if resolved is not None:
        return resolved
    raise _ConfigError(
        "no backup store configured. Set `backup_store` in "
        "~/.config/lazyserver/config.toml or pass --store <path>."
    )


def _select_entries(
    context: AppContext, entry_ids: list[str] | None
) -> tuple[list[ResolvedEntry], set[str]]:
    """Return (resolved_entries, unknown_ids). Resolution failures are
    logged + dropped — same posture as the TUI's per-entry resolution.

    When ``entry_ids`` is None or empty, every entry in the context is
    considered (the ``--all`` / ``--list`` case).
    """
    wanted = set(entry_ids) if entry_ids else None
    unknown: set[str] = set()
    if wanted is not None:
        known = {e.id for e in context.entries}
        unknown = wanted - known
        if unknown:
            return [], unknown

    candidates = [
        e for e in context.entries if wanted is None or e.id in wanted
    ]
    resolved: list[ResolvedEntry] = []
    for entry in candidates:
        try:
            resolved.append(
                resolve(entry, context.distro.id, target_user=context.target_user)
            )
        except ResolutionError as exc:
            log.warning("skip %s: %s", entry.id, exc)
    return resolved, set()
