"""Restore screen — pure-helper tests + Pilot smoke (FR-3, Phase 5).

Mirrors test_backup_screen: the rendering primitives (snapshot row
formatting, planned-row ownership preview, undo banner) get direct
unit tests; the navigation/action flow gets one Pilot end-to-end that
exercises home → r → snapshot pick → R → result modal.

The properties we lock in here are the two the VM test needs to find
green:

  * Pre-resolved ownership is shown on the files screen **before** R
    is pressed — the on-screen preview matches what the executor would
    write (FR-3.3).

  * The post-action modal contains the literal
    ``lsrv restore --entry ID --snapshot <ts>-pre-restore`` line, so
    the undo round-trip is verifiable from inside the TUI.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext, LazyServerApp
from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.restore import (
    FileSetExtra,
    PRE_RESTORE_SUFFIX,
    RestoreItem,
    SnapshotChoice,
)
from lazyserver.backup.run import backup_pending
from lazyserver.backup.store import FileMetadata, SnapshotRef
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.loader import load_folders
from lazyserver.tconf.resolve import resolve as resolve_entry
from lazyserver.ui.restore_screen import (
    ExtraRow,
    ItemSelection,
    PlannedRow,
    RestoreFilesScreen,
    RestoreSnapshotsScreen,
    SnapshotRow,
    _RestoreReportModal,
    build_planned_rows,
    format_undo_banner,
    list_snapshot_rows,
)


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _ubuntu() -> Distro:
    return Distro(
        id="ubuntu",
        pretty_name="Ubuntu 24.04",
        raw_id="ubuntu",
        raw_id_like=(),
        inferred=False,
    )


# ---------- SnapshotRow.label ----------


def test_snapshot_row_label_plural_files():
    r = SnapshotRow(timestamp="20260620-100000", file_count=3, is_pre_restore=False)
    assert "20260620-100000" in r.label
    assert "3 files" in r.label
    assert "PRE-RESTORE" not in r.label


def test_snapshot_row_label_singular_file():
    r = SnapshotRow(timestamp="t", file_count=1, is_pre_restore=False)
    assert "1 file" in r.label
    assert "1 files" not in r.label


def test_snapshot_row_label_marks_pre_restore():
    """Pre-restore snapshots must be visually distinguishable — they
    are the user's undo handles and conflating them with regular
    snapshots would be a foot-gun."""
    r = SnapshotRow(timestamp="t-pre-restore", file_count=2, is_pre_restore=True)
    assert "PRE-RESTORE" in r.label


# ---------- list_snapshot_rows ----------


def _seed_store(tmp_path: Path, entry_id: str = "smoke") -> tuple[Path, AppContext]:
    body = f"""
schema_version: 1
id: {entry_id}
name: Smoke
kind: service
description: x
files:
  - id: conf
    path: {tmp_path / 'smoke.conf'}
    description: smoke
distros:
  ubuntu:
    package: smoke
    service_unit: smoke
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / f"{entry_id}.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "smoke.conf").write_text("v1", encoding="utf-8")
    report = load_folders([folder])

    store_path = tmp_path / "store"
    ctx = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=str(store_path)),
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )
    return store_path, ctx


def test_list_snapshot_rows_returns_oldest_first_with_pre_restore_flagged(tmp_path):
    """Real-store integration: snapshots come back oldest → newest,
    and pre-restore directories are flagged in the resulting rows."""
    store_path, ctx = _seed_store(tmp_path)
    store_path.mkdir()
    resolved = [
        resolve_entry(e, "ubuntu", target_user=_self_user()) for e in ctx.entries
    ]
    store = PlainBackupStore(root=store_path, target_user=_self_user())
    baselines = BaselineStore.load(store_path, target_user=_self_user())

    # First snapshot.
    backup_pending(entries=resolved, store=store, baselines=baselines, timestamp="t1")
    # Edit + second snapshot.
    (tmp_path / "smoke.conf").write_text("v2")
    backup_pending(entries=resolved, store=store, baselines=baselines, timestamp="t2")
    # Lay down a pre-restore-like dir by manually invoking snapshot with the suffix.
    (tmp_path / "smoke.conf").write_text("v3")
    src = tmp_path / "smoke.conf"
    st = src.stat()
    store.snapshot(
        entry_id="smoke",
        source=src,
        timestamp=f"t3{PRE_RESTORE_SUFFIX}",
        metadata=FileMetadata(
            uid=st.st_uid, gid=st.st_gid, mode=st.st_mode & 0o7777, sha256="x" * 64
        ),
    )

    rows = list_snapshot_rows(store, "smoke")
    assert [r.timestamp for r in rows] == ["t1", "t2", f"t3{PRE_RESTORE_SUFFIX}"]
    assert [r.is_pre_restore for r in rows] == [False, False, True]
    # The pre-restore row's label carries the marker.
    assert "PRE-RESTORE" in rows[2].label


# ---------- build_planned_rows ----------


def test_build_planned_rows_pre_resolves_ownership_from_metadata():
    """The screen previews the exact (uid, gid, mode) the executor
    will apply — that comes from the captured metadata when present.
    Locking this proves the preview matches reality (FR-3.3)."""
    from lazyserver.backup.restore import RestorePlan

    item = RestoreItem(
        entry_id="smoke",
        snapshot="t1",
        source_path=Path("/tmp/x.conf"),
        ref=SnapshotRef(
            entry_id="smoke",
            timestamp="t1",
            source_path=Path("/tmp/x.conf"),
            stored_path=Path("/tmp/store/smoke/t1/tmp/x.conf"),
        ),
        file_id="conf",
        set_id=None,
        captured_metadata=FileMetadata(
            uid=121, gid=127, mode=0o640, sha256="a" * 64
        ),
    )
    plan = RestorePlan(items=(item,), extras=(), missing_entries=())
    planned, extras = build_planned_rows(
        plan, resolved_entries={}, target_user=_self_user()
    )
    assert len(planned) == 1
    row = planned[0]
    assert row.ownership.uid == 121
    assert row.ownership.gid == 127
    assert row.ownership.mode == 0o640
    # Preview label carries the resolved triple so the student sees it.
    assert "uid=121" in row.label
    assert "gid=127" in row.label
    assert "0o640" in row.label
    assert extras == []


def test_extras_row_label_marks_not_touched():
    """FR-3.4 extras must be visually flagged 'not touched' — silently
    listing them would invite the student to assume they'll be
    restored along with everything else."""
    row = ExtraRow(extra=FileSetExtra(entry_id="zoned", set_id="zones", path=Path("/etc/bind/db.added")))
    label = row.label
    assert "EXT" in label
    assert "not touched" in label
    assert "/etc/bind/db.added" in label
    assert "FR-3.4" in label


# ---------- format_undo_banner ----------


def test_undo_banner_contains_literal_cli_invocation():
    """The banner must yield the exact line the user can copy-paste,
    so the undo round-trip is verifiable from inside the TUI just
    like from the CLI."""
    lines = format_undo_banner("20260620-153045-pre-restore", ["bind9"])
    joined = "\n".join(lines)
    assert "Pre-restore snapshot: 20260620-153045-pre-restore" in joined
    assert (
        "lsrv restore --entry bind9 --snapshot 20260620-153045-pre-restore" in joined
    )


def test_undo_banner_one_line_per_entry():
    lines = format_undo_banner("ts-pre-restore", ["a", "b"])
    assert sum(1 for ln in lines if ln.lstrip().startswith("lsrv restore --entry")) == 2


# ---------- ItemSelection (pure) ----------


def test_item_selection_toggle_adds_then_removes():
    sel = ItemSelection()
    p = Path("/etc/bind/named.conf")
    sel.toggle(p)
    assert sel.is_selected(p)
    sel.toggle(p)
    assert not sel.is_selected(p)
    assert sel.count() == 0


def test_item_selection_select_all_unions():
    sel = ItemSelection()
    sel.toggle(Path("/a"))
    sel.select_all([Path("/a"), Path("/b"), Path("/c")])
    assert sel.count() == 3
    assert sel.is_selected(Path("/c"))


def test_item_selection_clear_resets():
    sel = ItemSelection()
    sel.select_all([Path("/a"), Path("/b")])
    sel.clear()
    assert sel.count() == 0


# ---------- Pilot smoke: home → r → enter → R ----------


def _seed_real_snapshot(tmp_path: Path) -> tuple[Path, AppContext]:
    """Lay down a real snapshot in the store so the screens can show
    something meaningful end-to-end."""
    store_path, ctx = _seed_store(tmp_path)
    store_path.mkdir()
    resolved = [
        resolve_entry(e, "ubuntu", target_user=_self_user()) for e in ctx.entries
    ]
    store = PlainBackupStore(root=store_path, target_user=_self_user())
    baselines = BaselineStore.load(store_path, target_user=_self_user())
    backup_pending(
        entries=resolved, store=store, baselines=baselines, timestamp="20260620-100000"
    )
    # Live edit that the restore will undo.
    (tmp_path / "smoke.conf").write_text("edited-by-mistake", encoding="utf-8")
    return store_path, ctx


async def test_pilot_home_r_opens_snapshots_screen(tmp_path):
    """`r` on HomeScreen (with an entry focused) opens the snapshots
    list for that entry — not a flattened "latest" view."""
    store_path, ctx = _seed_real_snapshot(tmp_path)
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RestoreSnapshotsScreen)
        # The snapshot is listed.
        view = app.screen.query_one("#restore-snapshots-list")
        rows = list(view.children)
        assert len(rows) == 1
        assert "20260620-100000" in str(rows[0].children[0].content)


async def test_pilot_snapshot_enter_opens_files_screen_with_ownership_preview(tmp_path):
    """Drilling into a snapshot shows the file list with resolved
    ownership inline — the FR-3.3 preview the VM test needs to see."""
    store_path, ctx = _seed_real_snapshot(tmp_path)
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RestoreFilesScreen)
        view = app.screen.query_one("#restore-files-list")
        rows = list(view.children)
        assert rows, "files screen must list the snapshot files"
        label_text = str(rows[0].children[0].content)
        assert "RST" in label_text
        # The pre-resolved ownership is in the row text — student can
        # verify uid/gid/mode before pressing R.
        assert "uid=" in label_text
        assert "mode=" in label_text


async def test_pilot_R_restores_and_modal_shows_undo_command(tmp_path, monkeypatch):
    """Press R: file is overwritten back to the snapshot version, the
    pre-restore snapshot lands in the store, and the result modal
    shows the literal `lsrv restore --entry ... --snapshot ...` line."""
    store_path, ctx = _seed_real_snapshot(tmp_path)

    # Pin the pre-restore timestamp so we can assert on the exact value.
    from lazyserver.ui import restore_screen as restore_screen_mod

    monkeypatch.setattr(
        restore_screen_mod, "current_timestamp", lambda: "20260620-153045"
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()

        # Live file was rewound.
        src = tmp_path / "smoke.conf"
        assert src.read_text() == "v1"
        # Pre-restore snapshot exists at the expected dir.
        pre_dir = store_path / "smoke" / f"20260620-153045{PRE_RESTORE_SUFFIX}"
        assert pre_dir.is_dir()
        # The pre-restore snapshot captured the edited content.
        pre_file = pre_dir / Path(*src.parts[1:])
        assert pre_file.read_text() == "edited-by-mistake"
        # The modal is now on top — and its children include the
        # literal undo command line.
        assert isinstance(app.screen, _RestoreReportModal)
        rendered = " ".join(
            str(getattr(w, "content", w.render()))
            for w in app.screen.query("Static")
        )
        assert f"20260620-153045{PRE_RESTORE_SUFFIX}" in rendered
        assert (
            f"lsrv restore --entry smoke --snapshot 20260620-153045{PRE_RESTORE_SUFFIX}"
            in rendered
        )


async def test_pilot_snapshots_screen_shows_no_snapshots_message(tmp_path):
    """No snapshots yet → friendly empty state, no crash."""
    store_path, ctx = _seed_store(tmp_path)
    # Note: store deliberately not seeded.
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RestoreSnapshotsScreen)
        empty = app.screen.query_one("#restore-empty-state")
        assert "No snapshots" in str(empty.content)


async def test_pilot_restore_screen_shows_no_store_message(tmp_path):
    """Without backup_store configured → clean hint, no traceback."""
    _, ctx_with = _seed_store(tmp_path)
    ctx = AppContext(
        target_user=ctx_with.target_user,
        settings=Settings(backup_store=None),
        distro=ctx_with.distro,
        entries=ctx_with.entries,
        shadowed=(),
    )
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RestoreSnapshotsScreen)
        empty = app.screen.query_one("#restore-empty-state")
        assert "No backup store" in str(empty.content)


def _seed_multi_file_snapshot(tmp_path: Path) -> tuple[Path, AppContext, Path, Path]:
    """Build an entry with two managed files, snapshot both, then edit
    both. Returns the store path, context, and both source paths.

    Models the bug from the VM screenshot: snapshot holds N files, the
    student broke only one, and the screen must let them restore just
    that one rather than overwriting the other N-1 they didn't touch.
    """
    body = f"""
schema_version: 1
id: zoned
name: Zoned
kind: service
description: x
files:
  - id: a
    path: {tmp_path / 'file-a.conf'}
    description: a
  - id: b
    path: {tmp_path / 'file-b.conf'}
    description: b
distros:
  ubuntu:
    package: zoned
    service_unit: zoned
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "zoned.yaml").write_text(body, encoding="utf-8")
    file_a = tmp_path / "file-a.conf"
    file_b = tmp_path / "file-b.conf"
    file_a.write_text("a-original", encoding="utf-8")
    file_b.write_text("b-original", encoding="utf-8")
    report = load_folders([folder])

    store_path = tmp_path / "store"
    ctx = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=str(store_path)),
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )
    store_path.mkdir()
    resolved = [
        resolve_entry(e, "ubuntu", target_user=_self_user()) for e in ctx.entries
    ]
    store = PlainBackupStore(root=store_path, target_user=_self_user())
    baselines = BaselineStore.load(store_path, target_user=_self_user())
    backup_pending(
        entries=resolved,
        store=store,
        baselines=baselines,
        timestamp="20260620-100000",
    )
    # Edit both — only one will be restored by the surgical test.
    file_a.write_text("a-edited", encoding="utf-8")
    file_b.write_text("b-edited", encoding="utf-8")
    return store_path, ctx, file_a, file_b


async def test_pilot_space_toggles_planned_row_marker(tmp_path):
    """Space on a focused RST row flips its `[ ]` marker to `[x]`,
    so the student can see what's picked before pressing R."""
    store_path, ctx, file_a, file_b = _seed_multi_file_snapshot(tmp_path)
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # First RST row starts with `[ ]`.
        view = app.screen.query_one("#restore-files-list")
        rows = list(view.children)
        before = str(rows[0]._label.content)
        assert "[ ]" in before
        await pilot.press("space")
        await pilot.pause()
        after = str(rows[0]._label.content)
        assert "[x]" in after


async def test_pilot_R_with_one_selected_only_restores_that_file(tmp_path, monkeypatch):
    """The VM-screenshot bug: snapshot holds 2 files, student only
    broke one. With one row selected, R must overwrite only that
    file and leave the other on-disk edit intact."""
    store_path, ctx, file_a, file_b = _seed_multi_file_snapshot(tmp_path)

    from lazyserver.ui import restore_screen as restore_screen_mod
    monkeypatch.setattr(
        restore_screen_mod, "current_timestamp", lambda: "20260620-153045"
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Rows are sorted by path: file-a.conf, then file-b.conf.
        # Focus is on the first row; toggle it.
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        # file-a was restored back to original.
        assert file_a.read_text() == "a-original"
        # file-b was NOT touched — student's other edit survives.
        assert file_b.read_text() == "b-edited"


async def test_pilot_R_with_no_selection_restores_all(tmp_path, monkeypatch):
    """With nothing selected, R falls back to the existing "restore
    everything visible" behavior — the common "fix this entry" flow."""
    store_path, ctx, file_a, file_b = _seed_multi_file_snapshot(tmp_path)

    from lazyserver.ui import restore_screen as restore_screen_mod
    monkeypatch.setattr(
        restore_screen_mod, "current_timestamp", lambda: "20260620-153045"
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # No space presses — selection is empty.
        await pilot.press("R")
        await pilot.pause()
        assert file_a.read_text() == "a-original"
        assert file_b.read_text() == "b-original"


async def test_pilot_a_selects_all_then_n_clears(tmp_path):
    """`a` flips every RST marker to `[x]`; `n` flips them back to `[ ]`."""
    store_path, ctx, file_a, file_b = _seed_multi_file_snapshot(tmp_path)
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("a")
        await pilot.pause()
        view = app.screen.query_one("#restore-files-list")
        for row in view.children:
            assert "[x]" in str(row._label.content)

        await pilot.press("n")
        await pilot.pause()
        for row in view.children:
            assert "[ ]" in str(row._label.content)


async def test_pilot_dry_run_does_not_overwrite(tmp_path, monkeypatch):
    """Toggling dry-run (`d`) must turn R into a no-op preview — live
    file untouched, no pre-restore snapshot written."""
    store_path, ctx = _seed_real_snapshot(tmp_path)
    src = tmp_path / "smoke.conf"

    app = LazyServerApp(ctx, dry_run=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        # Live file still has the edit, no pre-restore dir.
        assert src.read_text() == "edited-by-mistake"
        pre_dirs = [
            d for d in (store_path / "smoke").iterdir() if d.name.endswith(PRE_RESTORE_SUFFIX)
        ]
        assert pre_dirs == []
