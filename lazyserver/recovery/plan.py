"""Recovery planner — pure (FR-5.3, Phase 6).

Given the resolved entries on a fresh box and the cloned backup store,
produce a static plan: for each entry, what ``install`` / ``restore`` /
``enable`` would do, and the entries to act on in deterministic order.

Pure on purpose: the planner does not invoke anything. The same plan
feeds the non-interactive ``lsrv recover --all`` CLI, the interactive
TUI, and ``--dry-run`` rendering of all three. Failure-handling and
the per-entry cascade (install-fail → skip restore + enable) live in
the orchestrator (``recovery/run.py``, future commit), not here.

**Ordering.** Alphabetical by entry id, not dependency-aware. The
plan carries an ``ordering_note`` so the report can surface this
honestly to the operator (FR-5.3.2 says the human log explains
"everything done", and how we ordered work is part of that). A
dependency DAG is deferred until a real cross-entry need shows up.

**Backup store is ground truth.** An entry with zero deliberate
snapshots was never actually managed on this system — the tconf
catalogue may list it, but ``lsrv backup`` never captured it, so
recovery has no evidence it was here to put back. Recovery is *not*
first-time provisioning: for those entries the planner skips the
whole entry (install + restore + enable) with a single reason, rather
than installing a catalogue service the operator never used. This
avoids the concrete failure mode where a Debian catalogue entry
(e.g. Postfix) would prompt on install and hang a non-interactive
``recover --all``. The rule is "zero deliberate snapshots = skip
entry"; partial coverage (some files backed up, others not) still
recovers what the last snapshot holds.

**Latest snapshot per entry.** Pre-restore snapshots are skipped when
picking the restore target: they are undo handles for a previous
restore, not the user's last deliberate backup. Recovering from one
would mean "put me back to the broken thing I was undoing" — never
what we want on a fresh box. An entry whose *only* snapshots are
pre-restore handles therefore has zero deliberate snapshots and falls
into the whole-entry skip above.

**The ``enable`` step uses ``enable_now``** (``systemctl enable --now``)
so recovery enables-for-autostart AND starts the service in one shot,
matching FR-5.3.1's "enable/start". Plain ``enable`` is left untouched
so the EntryScreen's per-action UX (FR-1.5) does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..backup.restore import PRE_RESTORE_SUFFIX
from ..backup.store import BackupStore
from ..tconf.model import KIND_SERVICE
from ..tconf.resolve import ResolvedEntry

ORDERING_NOTE = (
    "Entries are processed in alphabetical order by id. "
    "Ordering is not dependency-aware; if one entry depends on another "
    "being installed first, that ordering is not enforced."
)

# Reason attached to every step of an entry that has zero deliberate
# snapshots in the store. Deliberately distinct from the old
# restore-only "no snapshots in store for this entry" message: the
# report should read "we did not touch this entry at all" (deliberate,
# whole-entry skip), not "install ran, restore did not" (half-run).
NO_BACKUPS_REASON = "no backups — entry not part of this system"


@dataclass(frozen=True)
class InstallStep:
    """Static plan for the ``install`` step of one entry.

    ``applicable=False`` means the orchestrator will not run anything
    for this step — usually because the entry declares no installable
    package (user has already installed it themselves).
    """

    applicable: bool
    argv: tuple[str, ...] | None
    reason_skipped: str | None = None


@dataclass(frozen=True)
class RestoreStep:
    """Static plan for the ``restore`` step of one entry.

    ``snapshot`` is the timestamp the orchestrator would restore from;
    ``file_count`` is what that snapshot holds. Both are pre-computed
    here so dry-run rendering and TUI previews do not re-walk the
    store at display time.
    """

    applicable: bool
    snapshot: str | None
    file_count: int
    reason_skipped: str | None = None


@dataclass(frozen=True)
class EnableStep:
    """Static plan for the ``enable`` step of one entry.

    Carries the resolved ``enable_now`` argv. Apps (no ``service_unit``)
    are marked not applicable — there is no service unit to enable.
    """

    applicable: bool
    argv: tuple[str, ...] | None
    reason_skipped: str | None = None


@dataclass(frozen=True)
class EntryPlan:
    """All three steps for one entry, plus identifying metadata.

    ``is_service`` is surfaced so the report can title-case the section
    differently for apps vs services without re-reading the tconf.
    """

    entry_id: str
    entry_name: str
    is_service: bool
    install: InstallStep
    restore: RestoreStep
    enable: EnableStep


@dataclass(frozen=True)
class RecoveryPlan:
    """The static recovery plan, deterministic for one (entries, store) input."""

    distro_id: str
    ordering_note: str
    entries: tuple[EntryPlan, ...]


def build_recovery_plan(
    *,
    resolved_entries: Iterable[ResolvedEntry],
    store: BackupStore,
    distro_id: str,
) -> RecoveryPlan:
    """Build the static plan for ``lsrv recover --all``.

    No I/O beyond the snapshot-listing reads needed to find the latest
    deliberate snapshot per entry. No subprocess. Safe to call from
    ``--dry-run`` and from the TUI's preview path.
    """
    ordered = sorted(resolved_entries, key=lambda r: r.entry.id)
    plans = tuple(_plan_one(re, store) for re in ordered)
    return RecoveryPlan(
        distro_id=distro_id,
        ordering_note=ORDERING_NOTE,
        entries=plans,
    )


def _plan_one(resolved: ResolvedEntry, store: BackupStore) -> EntryPlan:
    is_service = resolved.entry.kind == KIND_SERVICE
    snapshot = _latest_deliberate_snapshot(store, resolved.entry.id)
    if snapshot is None:
        # Zero deliberate snapshots: no evidence this entry was ever
        # part of this system. Skip all three steps with one reason so
        # the report reads as a deliberate skip, not a half-run.
        return EntryPlan(
            entry_id=resolved.entry.id,
            entry_name=resolved.entry.name,
            is_service=is_service,
            install=InstallStep(
                applicable=False, argv=None, reason_skipped=NO_BACKUPS_REASON
            ),
            restore=RestoreStep(
                applicable=False,
                snapshot=None,
                file_count=0,
                reason_skipped=NO_BACKUPS_REASON,
            ),
            enable=EnableStep(
                applicable=False, argv=None, reason_skipped=NO_BACKUPS_REASON
            ),
        )
    return EntryPlan(
        entry_id=resolved.entry.id,
        entry_name=resolved.entry.name,
        is_service=is_service,
        install=_plan_install(resolved),
        restore=_plan_restore(resolved, store, snapshot=snapshot),
        enable=_plan_enable(resolved),
    )


def _plan_install(resolved: ResolvedEntry) -> InstallStep:
    argv = resolved.install
    if not argv:
        return InstallStep(
            applicable=False,
            argv=None,
            reason_skipped="entry declares no install command",
        )
    return InstallStep(applicable=True, argv=argv)


def _plan_restore(
    resolved: ResolvedEntry, store: BackupStore, *, snapshot: str
) -> RestoreStep:
    # Caller guarantees ``snapshot`` is a real deliberate timestamp;
    # the zero-snapshot case is handled at the entry level in
    # ``_plan_one`` (whole-entry skip with NO_BACKUPS_REASON).
    file_count = len(store.list_files(resolved.entry.id, snapshot))
    return RestoreStep(
        applicable=True,
        snapshot=snapshot,
        file_count=file_count,
    )


def _plan_enable(resolved: ResolvedEntry) -> EnableStep:
    argv = resolved.actions.get("enable_now")
    if not argv:
        if resolved.entry.kind != KIND_SERVICE:
            reason = "entry is an app (no service to enable)"
        else:
            # Service entry but no enable_now — likely no service_unit
            # resolved for this distro, or an entry that overrode the
            # actions map and dropped the systemd defaults.
            reason = "no `enable_now` action resolved for this entry"
        return EnableStep(applicable=False, argv=None, reason_skipped=reason)
    return EnableStep(applicable=True, argv=argv)


def _latest_deliberate_snapshot(store: BackupStore, entry_id: str) -> str | None:
    """Pick the newest snapshot that is *not* a pre-restore handle.

    Pre-restore snapshots capture the live state the user was about to
    overwrite when they pressed R. Restoring one during recovery would
    put the pre-overwrite state back — usually the broken edit the
    user was trying to undo. The user's "latest deliberate intent" is
    the most recent plain snapshot, so we walk backwards from newest
    until we find one without the pre-restore suffix.
    """
    snapshots = list(store.list_snapshots(entry_id))
    for ts in reversed(snapshots):
        if not ts.endswith(PRE_RESTORE_SUFFIX):
            return ts
    return None
