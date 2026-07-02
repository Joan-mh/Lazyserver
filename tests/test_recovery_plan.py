"""Recovery planner — pure unit tests (FR-5.3, Phase 6).

The planner is pure (no subprocess, no UI). We construct ResolvedEntry
instances directly — same shortcut used in test_pending_baselines —
because the planner cares about a small slice (id, kind, install,
actions, snapshots) and going through YAML+loader+resolver is noise
for these properties.

Snapshots are laid down on a real ``PlainBackupStore`` via direct
``mkdir`` rather than through ``backup_pending``: the planner only
reads the directory layout, and the layout is the documented contract
of the store (``<root>/<entry_id>/<timestamp>/...``).
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup.restore import PRE_RESTORE_SUFFIX
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.user import TargetUser
from lazyserver.recovery.plan import (
    NO_BACKUPS_REASON,
    ORDERING_NOTE,
    EnableStep,
    EntryPlan,
    InstallStep,
    RecoveryPlan,
    RestoreStep,
    build_recovery_plan,
)
from lazyserver.tconf.model import KIND_APP, KIND_SERVICE, Entry
from lazyserver.tconf.resolve import ResolvedEntry


# ---------- helpers ----------


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _entry(entry_id: str, kind: str = KIND_SERVICE, name: str | None = None) -> Entry:
    return Entry(
        schema_version=1,
        id=entry_id,
        name=name or entry_id.title(),
        kind=kind,
        description="",
        distros={},
    )


def _resolved(
    entry_id: str,
    *,
    kind: str = KIND_SERVICE,
    install: tuple[str, ...] = ("apt-get", "install", "-y", "pkg"),
    enable_now: tuple[str, ...] | None = ("systemctl", "enable", "--now", "unit"),
    service_unit: str | None = "unit",
) -> ResolvedEntry:
    actions: dict[str, tuple[str, ...]] = {}
    if enable_now is not None:
        actions["enable_now"] = enable_now
    return ResolvedEntry(
        entry=_entry(entry_id, kind=kind),
        distro_id="ubuntu",
        package="pkg",
        service_unit=service_unit,
        install=install,
        actions=actions,
        files=(),
        file_sets=(),
    )


def _store(tmp_path: Path) -> PlainBackupStore:
    root = tmp_path / "store"
    root.mkdir()
    return PlainBackupStore(root=root, target_user=_self_user())


def _make_snapshot(
    store: PlainBackupStore,
    entry_id: str,
    timestamp: str,
    *,
    file_names: tuple[str, ...] = ("a.conf",),
) -> None:
    """Lay down a snapshot directory by hand.

    The store's read-side methods (list_snapshots / list_files) walk
    the directory tree under ``<root>/<entry_id>/<timestamp>/`` and
    treat any non-``metadata.json`` regular file as a captured source.
    Putting the files at the top level of the snapshot dir is enough
    for ``list_files`` to count them.
    """
    snap_dir = store.root / entry_id / timestamp
    snap_dir.mkdir(parents=True)
    for name in file_names:
        (snap_dir / name).write_text("x", encoding="utf-8")


# ---------- ordering & note ----------


def test_plan_orders_entries_alphabetically_by_id(tmp_path):
    """Disaster recovery should be reproducible: identical inputs →
    identical artifact bytes. Sorting by id is the cheapest stable
    order; matters because the JSON summary lists entries in plan
    order and a diff of two runs should highlight real changes only."""
    store = _store(tmp_path)
    entries = [_resolved("zeta"), _resolved("alpha"), _resolved("middle")]
    plan = build_recovery_plan(
        resolved_entries=entries, store=store, distro_id="ubuntu"
    )
    assert [e.entry_id for e in plan.entries] == ["alpha", "middle", "zeta"]


def test_plan_carries_ordering_note(tmp_path):
    """Joan asked for the report to disclose that ordering is
    alphabetical/not dependency-aware. The planner carries the note
    so the report formatter does not hardcode its own copy."""
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("a")], store=store, distro_id="ubuntu"
    )
    assert plan.ordering_note == ORDERING_NOTE
    assert "alphabetical" in plan.ordering_note
    assert "not dependency-aware" in plan.ordering_note


def test_plan_carries_distro_id(tmp_path):
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("a")], store=store, distro_id="arch"
    )
    assert plan.distro_id == "arch"


# ---------- install step ----------


def test_install_step_applicable_when_argv_present(tmp_path):
    store = _store(tmp_path)
    # Seed a snapshot so the whole-entry skip does not fire; this test
    # is about the install step's own applicability, not backup gating.
    _make_snapshot(store, "bind9", "20260601-000000")
    plan = build_recovery_plan(
        resolved_entries=[
            _resolved("bind9", install=("apt-get", "install", "-y", "bind9"))
        ],
        store=store,
        distro_id="ubuntu",
    )
    step = plan.entries[0].install
    assert step.applicable is True
    assert step.argv == ("apt-get", "install", "-y", "bind9")
    assert step.reason_skipped is None


def test_install_step_not_applicable_when_no_argv(tmp_path):
    """An entry without install — e.g. a pre-installed tool the user
    is only managing config for — must not stall recovery. Snapshot is
    seeded so we exercise the per-step 'no install command' reason
    rather than the whole-entry no-backup skip."""
    store = _store(tmp_path)
    _make_snapshot(store, "preinstalled", "20260601-000000")
    plan = build_recovery_plan(
        resolved_entries=[_resolved("preinstalled", install=())],
        store=store,
        distro_id="ubuntu",
    )
    step = plan.entries[0].install
    assert step.applicable is False
    assert step.argv is None
    assert "no install command" in (step.reason_skipped or "")


# ---------- restore step ----------


def test_restore_step_picks_latest_snapshot(tmp_path):
    store = _store(tmp_path)
    _make_snapshot(store, "bind9", "20260101-000000", file_names=("a.conf",))
    _make_snapshot(store, "bind9", "20260601-000000", file_names=("a.conf", "b.conf"))
    plan = build_recovery_plan(
        resolved_entries=[_resolved("bind9")], store=store, distro_id="ubuntu"
    )
    step = plan.entries[0].restore
    assert step.applicable is True
    assert step.snapshot == "20260601-000000"
    assert step.file_count == 2


def test_restore_step_skips_pre_restore_snapshots_when_picking_latest(tmp_path):
    """Pre-restore snapshots are undo handles, not deliberate backups.
    Restoring one during recovery would put the broken state back —
    the planner walks past them to find the user's last real backup."""
    store = _store(tmp_path)
    _make_snapshot(store, "bind9", "20260101-000000")
    _make_snapshot(store, "bind9", "20260201-000000")
    # User then did a restore — leaving a pre-restore handle as the
    # chronologically newest entry. Recovery must ignore it.
    _make_snapshot(
        store,
        "bind9",
        f"20260301-000000{PRE_RESTORE_SUFFIX}",
        file_names=("a.conf",),
    )
    plan = build_recovery_plan(
        resolved_entries=[_resolved("bind9")], store=store, distro_id="ubuntu"
    )
    step = plan.entries[0].restore
    assert step.snapshot == "20260201-000000"


def test_only_pre_restore_snapshots_skips_whole_entry(tmp_path):
    """Pathological case: the only snapshot is a pre-restore handle.
    Pre-restore snapshots are undo handles, not deliberate backups, so
    an entry whose *only* snapshots are pre-restore ones has zero
    deliberate backups — the whole entry is skipped, not just restore.
    Using the pre-restore snapshot would put the broken pre-undo state
    back; installing anyway would treat recovery as provisioning."""
    store = _store(tmp_path)
    _make_snapshot(store, "bind9", f"20260301-000000{PRE_RESTORE_SUFFIX}")
    plan = build_recovery_plan(
        resolved_entries=[_resolved("bind9")], store=store, distro_id="ubuntu"
    )
    entry_plan = plan.entries[0]
    assert entry_plan.install.applicable is False
    assert entry_plan.install.reason_skipped == NO_BACKUPS_REASON
    assert entry_plan.restore.applicable is False
    assert entry_plan.restore.snapshot is None
    assert entry_plan.restore.reason_skipped == NO_BACKUPS_REASON
    assert entry_plan.enable.applicable is False
    assert entry_plan.enable.reason_skipped == NO_BACKUPS_REASON


def test_zero_snapshots_skips_whole_entry(tmp_path):
    """Backup store is ground truth: a catalogue entry that was never
    backed up was never actually part of this system. Recovery is not
    first-time provisioning, so the planner skips install + restore +
    enable with a single unified reason rather than installing a
    catalogue service the operator never used. This is the concrete
    fix for the Postfix/Squid stall seen on the first VM recovery
    test — those entries had definitions but zero snapshots, and the
    old behaviour installed them anyway."""
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("untouched")], store=store, distro_id="ubuntu"
    )
    entry_plan = plan.entries[0]
    # All three steps skipped with the same reason — one honest outcome.
    assert entry_plan.install.applicable is False
    assert entry_plan.install.argv is None
    assert entry_plan.install.reason_skipped == NO_BACKUPS_REASON
    assert entry_plan.restore.applicable is False
    assert entry_plan.restore.snapshot is None
    assert entry_plan.restore.file_count == 0
    assert entry_plan.restore.reason_skipped == NO_BACKUPS_REASON
    assert entry_plan.enable.applicable is False
    assert entry_plan.enable.argv is None
    assert entry_plan.enable.reason_skipped == NO_BACKUPS_REASON


def test_no_backups_reason_message_is_operator_readable(tmp_path):
    """Lock the exact reason string: the report renders this verbatim
    to the operator, so the wording is part of the UX. Distinct from
    the old restore-only 'no snapshots in store for this entry' — this
    reads as a deliberate whole-entry skip, not a half-run."""
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("untouched")], store=store, distro_id="ubuntu"
    )
    assert NO_BACKUPS_REASON == "no backups — entry not part of this system"
    assert plan.entries[0].install.reason_skipped == NO_BACKUPS_REASON


# ---------- enable step ----------


def test_enable_step_applicable_for_service_with_enable_now(tmp_path):
    store = _store(tmp_path)
    _make_snapshot(store, "bind9", "20260601-000000")
    plan = build_recovery_plan(
        resolved_entries=[
            _resolved(
                "bind9",
                enable_now=("systemctl", "enable", "--now", "bind9"),
            )
        ],
        store=store,
        distro_id="ubuntu",
    )
    step = plan.entries[0].enable
    assert step.applicable is True
    assert step.argv == ("systemctl", "enable", "--now", "bind9")


def test_enable_step_not_applicable_for_app(tmp_path):
    """Apps (e.g. neovim) have no service to enable. The plan reflects
    this so the report shows 'enable: n/a' instead of failing. Snapshot
    is seeded to keep the whole-entry no-backup skip from firing — this
    test is about the per-step 'app has no service' path."""
    store = _store(tmp_path)
    _make_snapshot(store, "neovim", "20260601-000000")
    plan = build_recovery_plan(
        resolved_entries=[
            _resolved("neovim", kind=KIND_APP, enable_now=None, service_unit=None)
        ],
        store=store,
        distro_id="ubuntu",
    )
    step = plan.entries[0].enable
    assert step.applicable is False
    assert step.argv is None
    assert "app" in (step.reason_skipped or "")


def test_enable_step_not_applicable_when_service_missing_enable_now(tmp_path):
    """Defensive: a service entry whose actions map dropped the
    systemd defaults (e.g. all-explicit overrides) ends up without
    enable_now. The planner reports the gap rather than crashing."""
    store = _store(tmp_path)
    _make_snapshot(store, "custom", "20260601-000000")
    plan = build_recovery_plan(
        resolved_entries=[_resolved("custom", enable_now=None)],
        store=store,
        distro_id="ubuntu",
    )
    step = plan.entries[0].enable
    assert step.applicable is False
    assert "enable_now" in (step.reason_skipped or "")


# ---------- end-to-end shape ----------


def test_full_entry_plan_carries_identity_and_kind(tmp_path):
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("bind9", kind=KIND_SERVICE)],
        store=store,
        distro_id="ubuntu",
    )
    entry_plan = plan.entries[0]
    assert isinstance(entry_plan, EntryPlan)
    assert entry_plan.entry_id == "bind9"
    assert entry_plan.entry_name == "Bind9"
    assert entry_plan.is_service is True
    assert isinstance(entry_plan.install, InstallStep)
    assert isinstance(entry_plan.restore, RestoreStep)
    assert isinstance(entry_plan.enable, EnableStep)


def test_plan_is_a_frozen_dataclass(tmp_path):
    """Plans are passed across CLI, TUI and report layers; immutability
    avoids accidental mutation across handoffs."""
    store = _store(tmp_path)
    plan = build_recovery_plan(
        resolved_entries=[_resolved("a")], store=store, distro_id="ubuntu"
    )
    assert isinstance(plan, RecoveryPlan)
    with pytest.raises(Exception):
        plan.entries = ()  # type: ignore[misc]
