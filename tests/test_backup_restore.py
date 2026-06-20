"""Restore core — planner, ownership resolution, executor (Phase 5).

The pure-logic parts (snapshot picking, no-delete arithmetic, ownership
source decision, dangerous-mode detection) get direct unit tests. The
executor — the only piece that overwrites live files — is exercised
through the public API against tmp_path "live" files, with two
safety-critical scenarios called out:

  * Pre-restore snapshot must precede overwrite. If it fails, the live
    file is left untouched (FR-3.2 is the entire reason NFR-2 lets us
    skip confirmations).

  * A genuinely metadata-less snapshot (manually laid out without
    metadata.json — bit-identical to pre-Phase-5 archives) must restore
    correctly via the FR-1.8 fallback chain.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup import restore as restore_mod
from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.restore import (
    FileSetExtra,
    OwnershipChoice,
    PRE_RESTORE_SUFFIX,
    RestoreItem,
    RestoreOutcome,
    RestorePlan,
    RestoreSelection,
    SRC_APP_TARGET_USER,
    SRC_FR18_FALLBACK_ROOT,
    SRC_FR18_SET_DIR,
    SRC_FR18_SIBLING,
    SRC_LIVE_STAT,
    SRC_METADATA,
    SnapshotChoice,
    execute_restore,
    looks_dangerous,
    plan_restore,
    resolve_ownership,
)
from lazyserver.backup.run import backup_pending, current_timestamp
from lazyserver.backup.store import FileMetadata
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.loader import load_folders
from lazyserver.tconf.model import KIND_APP, KIND_SERVICE
from lazyserver.tconf.resolve import resolve as resolve_entry


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(
        name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir)
    )


# ---------- looks_dangerous (pure) ----------


@pytest.mark.parametrize("mode", [0o644, 0o600, 0o640, 0o755, 0o400])
def test_looks_dangerous_safe_modes(mode):
    assert looks_dangerous(mode) is False


@pytest.mark.parametrize("mode", [0o000, 0o100, 0o077, 0o277])
def test_looks_dangerous_unreadable_by_owner(mode):
    assert looks_dangerous(mode) is True, oct(mode)


# ---------- SnapshotChoice (pure) ----------


def test_snapshot_choice_latest_picks_newest():
    c = SnapshotChoice.latest_all()
    assert c.pick("bind9", ["t1", "t2", "t3"]) == "t3"


def test_snapshot_choice_empty_returns_none():
    c = SnapshotChoice.latest_all()
    assert c.pick("bind9", []) is None


def test_snapshot_choice_explicit_wins():
    c = SnapshotChoice(timestamps={"bind9": "t2"})
    assert c.pick("bind9", ["t1", "t2", "t3"]) == "t2"


def test_snapshot_choice_missing_explicit_returns_none():
    """A "restore the version from yesterday" request whose timestamp
    doesn't exist must surface that — not silently slide to latest."""
    c = SnapshotChoice(timestamps={"bind9": "t9-nonexistent"})
    assert c.pick("bind9", ["t1", "t2"]) is None


# ---------- resolve_ownership (pure) ----------


def test_ownership_metadata_wins_when_present(tmp_path):
    meta = FileMetadata(uid=121, gid=127, mode=0o640, sha256="x" * 64)
    out = resolve_ownership(
        captured=meta,
        live_path=tmp_path / "missing",
        entry_kind=KIND_SERVICE,
        target_user=_self_user(),
        file_set=None,
    )
    assert (out.uid, out.gid, out.mode) == (121, 127, 0o640)
    assert out.source == SRC_METADATA
    assert out.warnings == ()


def test_ownership_metadata_dangerous_mode_attaches_warning(tmp_path):
    """Literal restore + warning — we don't clamp the mode, but we tell
    the student a 0000 file is probably a mistake (decision (d))."""
    meta = FileMetadata(uid=0, gid=0, mode=0o000, sha256="x" * 64)
    out = resolve_ownership(
        captured=meta, live_path=tmp_path / "x", entry_kind=KIND_SERVICE,
        target_user=_self_user(), file_set=None,
    )
    assert out.mode == 0o000
    assert any("no read access" in w for w in out.warnings)


def test_ownership_app_target_user_when_no_metadata(tmp_path):
    """Backward-compat: a pre-Phase-5 app snapshot resolves to the target
    user, mirroring FR-1.8."""
    out = resolve_ownership(
        captured=None,
        live_path=tmp_path / "init.lua",
        entry_kind=KIND_APP,
        target_user=_self_user(),
        file_set=None,
    )
    assert out.source == SRC_APP_TARGET_USER
    assert out.uid == _self_user().uid


def test_ownership_service_no_metadata_uses_live_stat(tmp_path):
    """Backward-compat for service entries: prefer the live file's
    current stat if present — that's the FR-1.11 preserve-across-edits
    invariant, so it'll be e.g. bind:bind already."""
    live = tmp_path / "named.conf"
    live.write_bytes(b"x")
    os.chmod(live, 0o640)
    out = resolve_ownership(
        captured=None,
        live_path=live,
        entry_kind=KIND_SERVICE,
        target_user=_self_user(),
        file_set=None,
    )
    assert out.source == SRC_LIVE_STAT
    assert out.mode == 0o640


def test_ownership_service_no_metadata_no_live_falls_back_to_fr18_sibling(tmp_path):
    """Backward-compat last-resort with no live file: FR-1.8 chain."""
    set_dir = tmp_path / "etc" / "bind"
    set_dir.mkdir(parents=True)
    sibling = set_dir / "db.example"
    sibling.write_bytes(b"x")
    os.chmod(sibling, 0o640)

    from lazyserver.tconf.resolve import ResolvedFileSet
    fs = ResolvedFileSet(
        id="zones", directory=str(set_dir), pattern="db.*",
        description="x", example=None, optional=True,
        owner=None, group=None, mode=None,
    )
    out = resolve_ownership(
        captured=None,
        live_path=tmp_path / "etc" / "bind" / "db.missing",
        entry_kind=KIND_SERVICE,
        target_user=_self_user(),
        file_set=fs,
    )
    assert out.source == SRC_FR18_SIBLING
    assert out.mode == 0o640


def test_ownership_fr18_fallback_root_carries_warning(tmp_path):
    """When the chain bottoms out at root:root, the same warning pattern
    as FR-1.8's create flow surfaces — service may not read the file."""
    set_dir = tmp_path / "empty-dir-no-stat-info"
    # We construct an FS pointing at a non-existent directory so the chain
    # has no sibling, no dir info, and no override → fallback-root.
    from lazyserver.tconf.resolve import ResolvedFileSet
    fs = ResolvedFileSet(
        id="x", directory=str(set_dir), pattern="*.conf",
        description="x", example=None, optional=True,
        owner=None, group=None, mode=None,
    )
    out = resolve_ownership(
        captured=None,
        live_path=tmp_path / "missing",
        entry_kind=KIND_SERVICE,
        target_user=_self_user(),
        file_set=fs,
    )
    assert out.source == SRC_FR18_FALLBACK_ROOT
    assert any("fallback to root:root" in w for w in out.warnings)


# ---------- planning ----------


def _build_resolved(tmp_path: Path, *, kind: str = "service") -> dict:
    """Synthesise a one-entry tconf folder + return the resolved entry by id."""
    body = f"""
schema_version: 1
id: smoke
name: Smoke
kind: {kind}
description: x
files:
  - id: conf
    path: {tmp_path / 'smoke.conf'}
    description: smoke conf
distros:
  ubuntu:
    package: smoke
{'    service_unit: smoke' if kind == 'service' else ''}
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "smoke.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])
    entry = next(iter(report.entries.values()))
    resolved = resolve_entry(entry, "ubuntu", target_user=_self_user())
    return {entry.id: resolved}


def _backup_one_round(tmp_path: Path, src: Path, store_root: Path, *, kind="service"):
    resolved_map = _build_resolved(tmp_path, kind=kind)
    store = PlainBackupStore(root=store_root, target_user=_self_user())
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    backup_pending(
        entries=list(resolved_map.values()),
        store=store,
        baselines=baselines,
        timestamp="t1",
    )
    return resolved_map, store


def test_plan_restore_latest_includes_one_item_per_snapshot_file(tmp_path):
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"v1")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map, store = _backup_one_round(tmp_path, src, store_root)

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.source_path == src
    assert item.snapshot == "t1"
    assert item.captured_metadata is not None


def test_plan_restore_reports_missing_when_entry_has_no_snapshots(tmp_path):
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"v1")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map = _build_resolved(tmp_path)
    store = PlainBackupStore(root=store_root, target_user=_self_user())

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    assert plan.items == ()
    assert plan.missing_entries == ("smoke",)


# ---------- no-delete (FR-3.4) ----------


def _build_resolved_with_set(tmp_path: Path) -> dict:
    set_dir = tmp_path / "zones"
    set_dir.mkdir(exist_ok=True)
    body = f"""
schema_version: 1
id: zoned
name: Zoned
kind: service
description: x
file_sets:
  - id: zones
    directory: {set_dir}
    pattern: "db.*"
    description: zone files
distros:
  ubuntu:
    package: zoned
    service_unit: zoned
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "zoned.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])
    entry = next(iter(report.entries.values()))
    return {entry.id: resolve_entry(entry, "ubuntu", target_user=_self_user())}


def test_file_set_extras_reported_not_deleted(tmp_path):
    """A live file matching the set's glob but absent from the snapshot
    is reported, NOT included in plan.items (i.e. won't be overwritten
    or deleted). FR-3.4 baseline behaviour."""
    set_dir = tmp_path / "zones"
    a = set_dir / "db.example"
    b = set_dir / "db.other"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"A")
    b.write_bytes(b"B")

    resolved_map = _build_resolved_with_set(tmp_path)
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = PlainBackupStore(root=store_root, target_user=_self_user())
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    backup_pending(
        entries=list(resolved_map.values()),
        store=store, baselines=baselines, timestamp="t1",
    )

    # User adds 'c' AFTER backup. It's a live file_set member not in the snapshot.
    c = set_dir / "db.added-later"
    c.write_bytes(b"C")

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("zoned",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    item_paths = {it.source_path for it in plan.items}
    assert c not in item_paths, "extras must not appear in items"
    extra_paths = {e.path for e in plan.extras}
    assert c in extra_paths
    assert a in item_paths and b in item_paths


# ---------- execute_restore: safety contract ----------


def test_pre_restore_snapshot_is_taken_before_overwrite(tmp_path):
    """After a successful restore, the pre-restore snapshot dir exists
    AND contains the pre-overwrite content. Ordering proven via content
    inspection — if the overwrite had run first, the pre-snapshot would
    capture restored bytes, not original."""
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"original")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map, store = _backup_one_round(tmp_path, src, store_root)

    # User edits the file after backup. Restore will overwrite this edit.
    src.write_bytes(b"edited-after-backup")

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan,
        store=store,
        baselines=baselines,
        resolved_entries=resolved_map,
        target_user=_self_user(),
        pre_restore_timestamp="t2",
    )

    assert all(r.outcome is RestoreOutcome.RESTORED for r in reports if r.item)
    # Live file = original (restored from t1).
    assert src.read_bytes() == b"original"
    # Pre-restore snapshot exists at t2-pre-restore and captures the EDIT.
    pre_path = store_root / "smoke" / f"t2{PRE_RESTORE_SUFFIX}" / Path(*src.parts[1:])
    assert pre_path.exists(), "pre-restore snapshot must precede overwrite"
    assert pre_path.read_bytes() == b"edited-after-backup"


def test_pre_snapshot_failure_aborts_overwrite(tmp_path):
    """If pre-restore snapshot fails, the live file is NOT touched.
    Without this, `restore` with no confirmation would happily overwrite
    a file it couldn't first preserve — violating FR-3.2 + NFR-2."""
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"original")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map, store = _backup_one_round(tmp_path, src, store_root)
    src.write_bytes(b"edited-after-backup")

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )

    class _SnapshotKiller:
        def __init__(self, inner):
            self.root = inner.root
            self._inner = inner

        def snapshot(self, **kw):
            if PRE_RESTORE_SUFFIX in kw["timestamp"]:
                raise OSError("synthetic: pre-restore disk full")
            return self._inner.snapshot(**kw)

        def list_snapshots(self, e): return self._inner.list_snapshots(e)
        def list_files(self, e, t): return self._inner.list_files(e, t)
        def read(self, ref): return self._inner.read(ref)
        def read_metadata(self, e, t): return self._inner.read_metadata(e, t)
        def commit_operation(self, *, message): return self._inner.commit_operation(message=message)

    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan,
        store=_SnapshotKiller(store),
        baselines=baselines,
        resolved_entries=resolved_map,
        target_user=_self_user(),
        pre_restore_timestamp="t2",
    )

    assert len(reports) == 1
    rep = reports[0]
    assert rep.outcome is RestoreOutcome.PRE_SNAPSHOT_FAILED
    assert "synthetic" in (rep.error or "")
    # Critical: live file untouched.
    assert src.read_bytes() == b"edited-after-backup"


def test_take_pre_restore_false_skips_snapshot_even_when_live_exists(tmp_path):
    """Phase 6 recovery passes take_pre_restore=False: on a fresh box,
    the live file is the stock vendor copy the package manager just
    laid down. Snapshotting that as a "pre-restore" handle would
    pollute the store with vendor bytes. The overwrite still happens
    (we want the user's config back), the pre-snapshot just does not."""
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"original")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map, store = _backup_one_round(tmp_path, src, store_root)
    # Stock vendor copy on disk after apt-get install.
    src.write_bytes(b"vendor-stock")

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan, store=store, baselines=baselines,
        resolved_entries=resolved_map, target_user=_self_user(),
        pre_restore_timestamp="t2",
        take_pre_restore=False,
    )
    # Restore still wrote the file.
    assert reports[0].outcome is RestoreOutcome.RESTORED
    assert reports[0].pre_snapshot_ref is None
    assert src.read_bytes() == b"original"
    # No pre-restore dir landed in the store.
    pre_dir = store_root / "smoke" / f"t2{PRE_RESTORE_SUFFIX}"
    assert not pre_dir.exists(), "take_pre_restore=False must not create the dir"


def test_restore_with_no_pre_existing_live_file_skips_pre_snapshot(tmp_path):
    """If the live file is gone (e.g. mid-recovery), there's nothing to
    pre-snapshot. Restore must still write the file fresh — there's
    nothing to undo."""
    src = tmp_path / "smoke.conf"
    src.write_bytes(b"original")
    store_root = tmp_path / "store"
    store_root.mkdir()
    resolved_map, store = _backup_one_round(tmp_path, src, store_root)
    src.unlink()
    assert not src.exists()

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan, store=store, baselines=baselines,
        resolved_entries=resolved_map, target_user=_self_user(),
        pre_restore_timestamp="t2",
    )
    assert reports[0].outcome is RestoreOutcome.RESTORED
    assert reports[0].pre_snapshot_ref is None
    assert src.read_bytes() == b"original"


# ---------- backward-compat with a REAL metadata-less snapshot ----------


def test_restore_works_on_metadata_less_snapshot(tmp_path):
    """Lay out a snapshot dir by hand — no metadata.json — bit-identical
    to what a pre-step-1 (Phase 4) archive looks like on the VM. Restore
    must succeed via the FR-1.8 fallback chain.

    This is the test the user emphasised: my VM has these snapshots,
    they must work without rewriting them.
    """
    src = tmp_path / "etc" / "smoke.conf"
    src.parent.mkdir()
    src.write_bytes(b"live-original")  # also serves as FR-1.8 live-stat source
    os.chmod(src, 0o640)

    # Hand-laid Phase-4-style snapshot: content file, no metadata.json.
    # Store layout puts source /a/b/c at <snap_dir>/a/b/c, so we mirror the
    # tmp_path-anchored path to make the round-trip match the resolved file.
    store_root = tmp_path / "store"
    snap_dir = store_root / "smoke" / "20251201-000000"
    relative = Path(*src.parts[1:])
    stored = snap_dir / relative
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"legacy-from-backup")
    # Sanity: no metadata.json in this snapshot.
    assert not (snap_dir / "metadata.json").exists()

    body = f"""
schema_version: 1
id: smoke
name: Smoke
kind: service
description: x
files:
  - id: conf
    path: {src}
    description: smoke conf
distros:
  ubuntu:
    package: smoke
    service_unit: smoke
"""
    folder = tmp_path / "tconf2"
    folder.mkdir()
    (folder / "smoke.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])
    entry = next(iter(report.entries.values()))
    resolved_map = {entry.id: resolve_entry(entry, "ubuntu", target_user=_self_user())}

    store = PlainBackupStore(root=store_root, target_user=_self_user())
    # Confirm the store sees no metadata for this snapshot.
    assert store.read_metadata("smoke", "20251201-000000") is None

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("smoke",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    assert len(plan.items) == 1
    assert plan.items[0].captured_metadata is None  # the backward-compat signal

    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan,
        store=store,
        baselines=baselines,
        resolved_entries=resolved_map,
        target_user=_self_user(),
        pre_restore_timestamp="t2",
    )
    rep = reports[0]
    assert rep.outcome is RestoreOutcome.RESTORED
    # Backward-compat path fired: ownership came from live stat (the FR-1.11
    # invariant means the live file still has the original owner/mode).
    assert rep.ownership_source == SRC_LIVE_STAT
    assert rep.chosen_mode == 0o640
    assert src.read_bytes() == b"legacy-from-backup"


# ---------- extras (FR-3.4) surface in the report ----------


def test_extras_appear_as_report_rows(tmp_path):
    set_dir = tmp_path / "zones"
    set_dir.mkdir()
    a = set_dir / "db.example"; a.write_bytes(b"A")
    resolved_map = _build_resolved_with_set(tmp_path)
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = PlainBackupStore(root=store_root, target_user=_self_user())
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    backup_pending(
        entries=list(resolved_map.values()),
        store=store, baselines=baselines, timestamp="t1",
    )

    # Add an extra file after backup.
    extra = set_dir / "db.added"; extra.write_bytes(b"E")

    plan = plan_restore(
        selection=RestoreSelection(
            entry_ids=("zoned",), file_paths=None,
            snapshot_choice=SnapshotChoice.latest_all(),
        ),
        resolved_entries=resolved_map,
        store=store,
    )
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    reports = execute_restore(
        plan, store=store, baselines=baselines,
        resolved_entries=resolved_map, target_user=_self_user(),
        pre_restore_timestamp="t2",
    )
    extra_rows = [r for r in reports if r.outcome is RestoreOutcome.EXTRA_REPORTED]
    assert len(extra_rows) == 1
    assert extra_rows[0].extra.path == extra
    # And the extra is still on disk — restore did not delete it.
    assert extra.exists()
    assert extra.read_bytes() == b"E"
