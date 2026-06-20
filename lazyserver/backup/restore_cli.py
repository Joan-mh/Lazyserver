"""Restore CLI dispatcher (FR-3 / Phase 5).

Three modes, mutually exclusive:

  ``--file PATH [--snapshot TS]``   restore one live file from a snapshot.
  ``--entry ID  [--snapshot TS]``   restore every file of one entry.
  ``--all``                          restore every entry's latest snapshot.

All three respect the global ``--dry-run``: the plan is printed (what
would overwrite, what extras would be reported per FR-3.4, the
pre-restore timestamp that would be captured) without writing anything.

Exit codes mirror the backup CLI so Phase 6 recovery can branch on them:

  0  ok
  1  hard error (no store configured, unknown entry id, file not owned
     by any entry, requested snapshot does not exist, nothing to do)
  2  partial failure (some items failed to pre-snapshot or overwrite)

**Why the pre-restore timestamp is printed loud.** FR-3.2 makes every
restore reversible: a per-entry ``<ts>-pre-restore`` snapshot of the
on-disk state is captured before any overwrite. That timestamp is the
user's undo handle — printing it as the last thing they see (with the
exact ``lsrv restore --entry ID --snapshot <ts>-pre-restore`` line)
turns "I just restored the wrong version" into a one-line fix instead
of a forensic dig through the store.
"""

from __future__ import annotations

import fnmatch
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from ..app import AppContext, BootstrapError, bootstrap
from ..config import Settings, expand_user_path, resolved_backup_store
from ..platform.user import TargetUser
from ..tconf.resolve import ResolutionError, ResolvedEntry, resolve
from .restore import (
    FileSetExtra,
    PRE_RESTORE_SUFFIX,
    RestoreOutcome,
    RestorePlan,
    RestoreReport,
    RestoreSelection,
    SnapshotChoice,
    execute_restore,
    plan_restore,
)
from .pending import BaselineStore
from .run import current_timestamp
from .store import BackupStore, make_backup_store

log = logging.getLogger("lazyserver.backup.restore_cli")

EXIT_OK = 0
EXIT_HARD_ERROR = 1
EXIT_PARTIAL_FAILURE = 2


# ---------- entry point ----------


def cmd_restore(
    *,
    file: Path | None,
    entry: str | None,
    all_entries: bool,
    snapshot: str | None,
    store_override: Path | None,
    dry_run: bool,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Argparse-friendly entry point. The top-level CLI dispatches here."""
    if all_entries and snapshot is not None:
        # Argparse can't express this cleanly across a mutex group, so
        # we enforce it here. The constraint exists because --all is
        # "per-entry latest"; an explicit timestamp would have to apply
        # uniformly to every entry, and the same string rarely exists
        # for more than one entry.
        print(
            "lazyserver: --snapshot is not allowed with --all "
            "(use --entry ID --snapshot TS to pin one entry).",
            file=err,
        )
        return EXIT_HARD_ERROR

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

    try:
        resolved_map, selection = _build_selection(
            context=context,
            file=file,
            entry=entry,
            all_entries=all_entries,
            snapshot=snapshot,
        )
    except _SelectionError as exc:
        print(f"lazyserver: {exc}", file=err)
        return EXIT_HARD_ERROR

    store = make_backup_store(store_path, target_user=context.target_user)

    plan = plan_restore(
        selection=selection,
        resolved_entries=resolved_map,
        store=store,
    )

    pre_restore_ts = current_timestamp()

    if dry_run:
        return _print_plan(
            plan, store_path, pre_restore_ts=pre_restore_ts, out=out, err=err
        )

    return _run_real(
        plan,
        store=store,
        baselines_root=store_path,
        target_user=context.target_user,
        resolved_map=resolved_map,
        pre_restore_ts=pre_restore_ts,
        store_path=store_path,
        out=out,
        err=err,
    )


# ---------- modes / selection ----------


class _SelectionError(RuntimeError):
    """Raised when --file/--entry/--all can't be turned into a plan."""


def _build_selection(
    *,
    context: AppContext,
    file: Path | None,
    entry: str | None,
    all_entries: bool,
    snapshot: str | None,
) -> tuple[dict[str, ResolvedEntry], RestoreSelection]:
    """Map the CLI flags to a RestoreSelection + resolved entries.

    Returns ``(resolved_map, selection)``. The resolved_map only
    contains the entries we actually want to touch — narrowing here
    keeps planning fast and avoids surprise extras from unrelated
    entries' file_sets when the user asked for one file.
    """
    if all_entries:
        resolved_map = _resolve_all(context)
        if not resolved_map:
            raise _SelectionError(
                "no entries resolved for this distro — nothing to restore."
            )
        return resolved_map, RestoreSelection(
            entry_ids=tuple(sorted(resolved_map)),
            file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        )

    if entry is not None:
        if entry not in {e.id for e in context.entries}:
            raise _SelectionError(f"unknown entry id: {entry}")
        try:
            resolved = resolve(
                next(e for e in context.entries if e.id == entry),
                context.distro.id,
                target_user=context.target_user,
            )
        except ResolutionError as exc:
            raise _SelectionError(
                f"entry {entry!r} cannot be resolved on {context.distro.id}: {exc}"
            ) from exc
        return {entry: resolved}, RestoreSelection(
            entry_ids=(entry,),
            file_paths=None,
            snapshot_choice=_snapshot_choice_for([entry], snapshot),
        )

    # --file PATH
    assert file is not None  # argparse guarantees one of the three
    path = file if file.is_absolute() else file.resolve()
    owner = _find_entry_for_file(context, path)
    if owner is None:
        raise _SelectionError(
            f"file {path} is not managed by any entry — declare it in tconf "
            f"(or use --entry to restore a different file)."
        )
    entry_id, resolved = owner
    return {entry_id: resolved}, RestoreSelection(
        entry_ids=(entry_id,),
        file_paths=(path,),
        snapshot_choice=_snapshot_choice_for([entry_id], snapshot),
    )


def _snapshot_choice_for(
    entry_ids: list[str], snapshot: str | None
) -> SnapshotChoice:
    if snapshot is None:
        return SnapshotChoice.latest_all()
    return SnapshotChoice(timestamps={eid: snapshot for eid in entry_ids})


def _resolve_all(context: AppContext) -> dict[str, ResolvedEntry]:
    """Resolve every entry; drop the ones that don't resolve on this distro.

    Matches the backup CLI's "skip unresolvable, don't abort" posture —
    one bad entry shouldn't block restoring the rest.
    """
    resolved: dict[str, ResolvedEntry] = {}
    for entry in context.entries:
        try:
            resolved[entry.id] = resolve(
                entry, context.distro.id, target_user=context.target_user
            )
        except ResolutionError as exc:
            log.warning("skip %s: %s", entry.id, exc)
    return resolved


def _find_entry_for_file(
    context: AppContext, path: Path
) -> tuple[str, ResolvedEntry] | None:
    """Return the (entry_id, resolved) that declares ``path``.

    Match order: exact fixed-file path first, then file_set
    directory+pattern. If two entries claim the same path we error —
    that's a tconf bug, not something the CLI should silently choose
    on the user's behalf.
    """
    matches: list[tuple[str, ResolvedEntry]] = []
    for entry in context.entries:
        try:
            resolved = resolve(
                entry, context.distro.id, target_user=context.target_user
            )
        except ResolutionError:
            continue
        if _entry_owns_path(resolved, path):
            matches.append((entry.id, resolved))
    if not matches:
        return None
    if len(matches) > 1:
        ids = ", ".join(m[0] for m in matches)
        raise _SelectionError(
            f"file {path} is claimed by multiple entries ({ids}); "
            f"use --entry to pick one."
        )
    return matches[0]


def _entry_owns_path(resolved: ResolvedEntry, path: Path) -> bool:
    for rf in resolved.files:
        if Path(rf.path) == path:
            return True
    for fs in resolved.file_sets:
        try:
            rel = path.relative_to(Path(fs.directory))
        except ValueError:
            continue
        if fnmatch.fnmatch(str(rel), fs.pattern):
            return True
    return False


# ---------- plan output (dry-run + --list-style) ----------


def _print_plan(
    plan: RestorePlan,
    store_path: Path,
    *,
    pre_restore_ts: str,
    out,
    err,
) -> int:
    """Render the plan without writing. Shared by --dry-run."""
    if not plan.items and not plan.extras and not plan.missing_entries:
        print("(nothing to restore — no snapshots match the selection.)", file=out)
        return EXIT_OK

    by_entry: dict[str, list[str]] = {}
    for item in plan.items:
        ts_tag = f"snapshot {item.snapshot}"
        by_entry.setdefault(item.entry_id, []).append(
            f"  OVERWRITE  {item.source_path}    [{ts_tag}]"
        )
    for extra in plan.extras:
        by_entry.setdefault(extra.entry_id, []).append(
            f"  EXTRA      {extra.path}    "
            f"[not in snapshot — will be reported, NOT deleted]"
        )

    for entry_id in sorted(by_entry):
        print(f"{entry_id}:", file=out)
        for line in by_entry[entry_id]:
            print(line, file=out)

    if plan.missing_entries:
        print(file=out)
        for missing in plan.missing_entries:
            print(
                f"⚠ {missing}: no matching snapshot — nothing to restore for this entry.",
                file=out,
            )

    print(file=out)
    overwrite_count = len(plan.items)
    extra_count = len(plan.extras)
    print(
        f"Plan: {overwrite_count} file(s) would be restored, "
        f"{extra_count} extra(s) would be reported. Store: {store_path}",
        file=out,
    )
    print(
        f"Pre-restore snapshot would be captured at: "
        f"{pre_restore_ts}{PRE_RESTORE_SUFFIX}",
        file=out,
    )
    if plan.missing_entries:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


# ---------- real execution ----------


def _run_real(
    plan: RestorePlan,
    *,
    store: BackupStore,
    baselines_root: Path,
    target_user: TargetUser,
    resolved_map: dict[str, ResolvedEntry],
    pre_restore_ts: str,
    store_path: Path,
    out,
    err,
) -> int:
    if not plan.items and not plan.extras and not plan.missing_entries:
        print("(nothing to restore — no snapshots match the selection.)", file=err)
        return EXIT_HARD_ERROR

    baselines = BaselineStore.load(baselines_root, target_user=target_user)
    reports = execute_restore(
        plan,
        store=store,
        baselines=baselines,
        resolved_entries=resolved_map,
        target_user=target_user,
        pre_restore_timestamp=pre_restore_ts,
    )
    return _print_report(
        reports,
        store_path=store_path,
        pre_restore_ts=pre_restore_ts,
        missing_entries=plan.missing_entries,
        out=out,
        err=err,
    )


def _print_report(
    reports: list[RestoreReport],
    *,
    store_path: Path,
    pre_restore_ts: str,
    missing_entries: tuple[str, ...],
    out,
    err,
) -> int:
    by_entry: dict[str, list[RestoreReport]] = {}
    for rep in reports:
        eid = rep.item.entry_id if rep.item else rep.extra.entry_id  # type: ignore[union-attr]
        by_entry.setdefault(eid, []).append(rep)

    restored = failed = extras = 0
    touched_entries: list[str] = []
    warnings: list[tuple[str, str]] = []  # (path, warning) for the summary block

    for entry_id in sorted(by_entry):
        rows = by_entry[entry_id]
        print(f"{entry_id}:", file=out)
        any_restored = False
        for rep in rows:
            icon, note, where = _outcome_glyph(rep)
            print(f"  {icon} {where}    {note}", file=out)
            for w in rep.warnings:
                warnings.append((where, w))
            if rep.outcome is RestoreOutcome.RESTORED:
                restored += 1
                any_restored = True
            elif rep.outcome in (
                RestoreOutcome.WRITE_FAILED,
                RestoreOutcome.PRE_SNAPSHOT_FAILED,
            ):
                failed += 1
            elif rep.outcome is RestoreOutcome.EXTRA_REPORTED:
                extras += 1
        if any_restored:
            touched_entries.append(entry_id)

    print(file=out)
    print(
        f"Restored {restored} file(s). "
        f"{extras} extra(s) reported (left in place, FR-3.4). "
        f"Store: {store_path}",
        file=out,
    )

    if warnings:
        print(file=out)
        print("Warnings:", file=out)
        for path, w in warnings:
            print(f"  ⚠ {path}: {w}", file=out)

    # Pre-restore timestamp banner — the FR-3.2 undo handle.
    if touched_entries:
        pre_ts = f"{pre_restore_ts}{PRE_RESTORE_SUFFIX}"
        print(file=out)
        print(f"Pre-restore snapshot: {pre_ts}", file=out)
        print("To undo this restore, re-run with --snapshot <ts>:", file=out)
        for entry_id in touched_entries:
            print(
                f"    lsrv restore --entry {entry_id} --snapshot {pre_ts}",
                file=out,
            )

    if missing_entries:
        print(file=out)
        for missing in missing_entries:
            print(
                f"⚠ {missing}: no matching snapshot — nothing was restored for this entry.",
                file=err,
            )

    if failed:
        print(f"⚠ {failed} file(s) failed — see lines marked ✗ above.", file=err)
        return EXIT_PARTIAL_FAILURE
    if missing_entries and restored == 0:
        return EXIT_HARD_ERROR
    if missing_entries:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def _outcome_glyph(rep: RestoreReport) -> tuple[str, str, str]:
    """Return (glyph, note, displayed-path) for one report row."""
    if rep.outcome is RestoreOutcome.RESTORED:
        assert rep.item is not None
        mode = oct(rep.chosen_mode) if rep.chosen_mode is not None else "?"
        note = (
            f"restored from {rep.item.snapshot}  "
            f"uid={rep.chosen_uid} gid={rep.chosen_gid} mode={mode}"
        )
        return "✓", note, str(rep.item.source_path)
    if rep.outcome is RestoreOutcome.PRE_SNAPSHOT_FAILED:
        assert rep.item is not None
        return (
            "✗",
            f"pre-restore snapshot failed: {rep.error or 'unknown'} — live file untouched",
            str(rep.item.source_path),
        )
    if rep.outcome is RestoreOutcome.WRITE_FAILED:
        assert rep.item is not None
        pre_note = ""
        if rep.pre_snapshot_ref is not None:
            pre_note = (
                f"  (pre-restore snapshot at "
                f"{rep.pre_snapshot_ref.timestamp} is intact)"
            )
        return (
            "✗",
            f"write failed: {rep.error or 'unknown'}{pre_note}",
            str(rep.item.source_path),
        )
    if rep.outcome is RestoreOutcome.EXTRA_REPORTED:
        assert rep.extra is not None
        return (
            "·",
            "extra (not in snapshot — left in place, FR-3.4)",
            str(rep.extra.path),
        )
    return "?", rep.outcome.value, ""


# ---------- helpers shared with backup CLI ----------


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
