"""Backup screen — pure-function tests + one Pilot smoke (4e, FR-2.3/2.4).

The Textual widget itself is exercised only by the smoke test; the
grouping rule, the visibility predicate, and the selection model are
covered by direct unit tests against the helper functions.
"""

from __future__ import annotations

import io
import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext, LazyServerApp
from lazyserver.backup.pending import (
    BASELINES_FILENAME,
    BaselineStore,
    PendingItem,
    PendingStatus,
)
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.loader import load_folders
from lazyserver.ui.backup_screen import (
    BackupScreen,
    EntryGroup,
    Selection,
    group_for_display,
    is_entry_actionable,
    latest_snapshot,
    summarize_group,
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


def _item(
    entry_id: str,
    path: str,
    status: PendingStatus,
    file_id: str = "f",
) -> PendingItem:
    return PendingItem(
        entry_id=entry_id,
        file_id=file_id,
        set_id=None,
        path=Path(path),
        status=status,
        current_sha=None,
        baseline_sha=None,
    )


# ---------- is_entry_actionable ----------


def test_entry_with_at_least_one_new_changed_or_missing_is_actionable():
    for s in (PendingStatus.NEW, PendingStatus.CHANGED, PendingStatus.MISSING):
        items = [_item("e", "/x", s), _item("e", "/y", PendingStatus.ABSENT_REQUIRED)]
        assert is_entry_actionable(items), s


def test_entirely_clean_entry_is_hidden():
    items = [
        _item("e", "/x", PendingStatus.UNCHANGED),
        _item("e", "/y", PendingStatus.ABSENT_OPTIONAL),
    ]
    assert not is_entry_actionable(items)


def test_entirely_absent_required_entry_is_hidden():
    """The 'package not installed' proxy: every declared file is
    ABSENT_REQUIRED. Should not appear on the backup screen."""
    items = [
        _item("e", "/x", PendingStatus.ABSENT_REQUIRED),
        _item("e", "/y", PendingStatus.ABSENT_REQUIRED),
    ]
    assert not is_entry_actionable(items)


def test_mixed_actionable_and_absent_entry_is_actionable():
    items = [
        _item("e", "/x", PendingStatus.CHANGED),
        _item("e", "/y", PendingStatus.ABSENT_REQUIRED),
    ]
    assert is_entry_actionable(items)


# ---------- group_for_display ----------


def test_group_drops_unchanged_and_absent_optional_rows():
    items = [
        _item("e", "/x", PendingStatus.NEW),
        _item("e", "/y", PendingStatus.UNCHANGED),
        _item("e", "/z", PendingStatus.ABSENT_OPTIONAL),
    ]
    groups = group_for_display(items, {"e": "Entry"})
    assert len(groups) == 1
    statuses = [i.status for i in groups[0].items]
    assert statuses == [PendingStatus.NEW]


def test_group_hides_uninstalled_proxy_entries():
    items = [
        _item("nginx", "/etc/nginx/nginx.conf", PendingStatus.ABSENT_REQUIRED),
        _item("bind9", "/etc/named.conf", PendingStatus.CHANGED),
    ]
    groups = group_for_display(items, {"nginx": "nginx", "bind9": "bind9"})
    assert {g.entry_id for g in groups} == {"bind9"}


def test_group_orders_entries_by_id_and_files_by_path():
    items = [
        _item("zeta", "/b", PendingStatus.NEW),
        _item("alpha", "/a/2", PendingStatus.NEW),
        _item("alpha", "/a/1", PendingStatus.CHANGED),
    ]
    groups = group_for_display(items, {"zeta": "Zeta", "alpha": "Alpha"})
    assert [g.entry_id for g in groups] == ["alpha", "zeta"]
    assert [str(i.path) for i in groups[0].items] == ["/a/1", "/a/2"]


# ---------- summarize_group ----------


def test_summarize_group_counts_per_status():
    g = EntryGroup(
        entry_id="bind9",
        entry_name="BIND9",
        items=[
            _item("bind9", "/a", PendingStatus.NEW),
            _item("bind9", "/b", PendingStatus.NEW),
            _item("bind9", "/c", PendingStatus.CHANGED),
            _item("bind9", "/d", PendingStatus.ABSENT_REQUIRED),
        ],
    )
    summary = summarize_group(g)
    assert "4 pending" in summary
    assert "2 new" in summary
    assert "1 changed" in summary
    assert "1 absent" in summary


# ---------- Selection ----------


def _group(entry_id: str, *items: PendingItem) -> EntryGroup:
    return EntryGroup(entry_id=entry_id, entry_name=entry_id.upper(), items=list(items))


def test_selection_toggle_file_only_works_for_eligible_items():
    sel = Selection()
    new_item = _item("e", "/x", PendingStatus.NEW)
    absent = _item("e", "/y", PendingStatus.ABSENT_REQUIRED)
    sel.toggle_file("e", new_item)
    sel.toggle_file("e", absent)  # silent no-op
    assert sel.is_selected("e", Path("/x"))
    assert not sel.is_selected("e", Path("/y"))
    assert sel.count() == 1


def test_selection_toggle_entry_fans_out_to_eligible_files_only():
    sel = Selection()
    g = _group(
        "e",
        _item("e", "/a", PendingStatus.NEW),
        _item("e", "/b", PendingStatus.CHANGED),
        _item("e", "/c", PendingStatus.ABSENT_REQUIRED),
    )
    sel.toggle_entry(g)
    assert sel.is_selected("e", Path("/a"))
    assert sel.is_selected("e", Path("/b"))
    assert not sel.is_selected("e", Path("/c"))
    # Toggling again deselects them all.
    sel.toggle_entry(g)
    assert sel.count() == 0


def test_selection_toggle_entry_when_partial_selects_remainder():
    """If some but not all of an entry's eligible files are selected,
    toggling the header fills in the rest rather than deselecting."""
    sel = Selection()
    g = _group(
        "e",
        _item("e", "/a", PendingStatus.NEW),
        _item("e", "/b", PendingStatus.CHANGED),
    )
    sel.toggle_file("e", g.items[0])  # /a only
    sel.toggle_entry(g)
    assert sel.is_selected("e", Path("/a"))
    assert sel.is_selected("e", Path("/b"))


def test_select_all_only_picks_eligible():
    sel = Selection()
    groups = [
        _group(
            "e1",
            _item("e1", "/a", PendingStatus.NEW),
            _item("e1", "/b", PendingStatus.MISSING),
        ),
        _group(
            "e2",
            _item("e2", "/c", PendingStatus.CHANGED),
            _item("e2", "/d", PendingStatus.ABSENT_REQUIRED),
        ),
    ]
    sel.select_all(groups)
    # NEW and CHANGED are eligible. MISSING and ABSENT_REQUIRED are not.
    selected_paths = {
        str(i.path) for i in sel.selected_items(groups)
    }
    assert selected_paths == {"/a", "/c"}


def test_selected_items_returns_pending_items_in_stable_order():
    sel = Selection()
    g = _group(
        "e",
        _item("e", "/a", PendingStatus.NEW),
        _item("e", "/b", PendingStatus.CHANGED),
    )
    sel.toggle_entry(g)
    items = sel.selected_items([g])
    assert [str(i.path) for i in items] == ["/a", "/b"]


# ---------- latest_snapshot ----------


def test_latest_snapshot_returns_max_across_entries(tmp_path: Path):
    from lazyserver.backup.pending import Baseline

    baselines = BaselineStore(root=None)
    baselines.set(
        "bind9",
        Path("/etc/named.conf"),
        Baseline(sha256="a" * 64, snapshot="20260101-000000", file_id="f", set_id=None),
    )
    baselines.set(
        "nginx",
        Path("/etc/nginx.conf"),
        Baseline(sha256="b" * 64, snapshot="20260601-120000", file_id="g", set_id=None),
    )
    assert latest_snapshot(baselines, ["bind9", "nginx"]) == "20260601-120000"


def test_latest_snapshot_none_on_empty_ledger():
    assert latest_snapshot(BaselineStore(root=None), ["whatever"]) is None


# ---------- Pilot smoke: home → b → space → b end-to-end ----------


def _context_with_one_pending_file(tmp_path: Path, store_path: Path) -> AppContext:
    body = f"""
schema_version: 1
id: smoke
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
    (folder / "smoke.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "smoke.conf").write_text("v1", encoding="utf-8")
    report = load_folders([folder])

    return AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=str(store_path)),
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )


async def test_pilot_home_to_backup_to_backup_action(tmp_path):
    store = tmp_path / "store"
    ctx = _context_with_one_pending_file(tmp_path, store)
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Open backup screen with the global 'b' binding.
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, BackupScreen)
        # The pending file is listed.
        view = app.screen.query_one("#backup-list")
        rows = list(view.children)
        # Two rows: entry header + file row.
        assert len(rows) == 2
        # Press B (uppercase = back up all) to skip selection.
        await pilot.press("B")
        await pilot.pause()
        # Snapshot landed.
        assert (store / "smoke").is_dir()
        assert (store / BASELINES_FILENAME).exists()
        # Re-scan happened: no rows left (the file is now backed up).
        result = app.screen.query_one("#backup-result")
        assert "Backed up 1" in str(result.content)


async def test_pilot_backup_screen_shows_no_store_message(tmp_path):
    """Without backup_store in settings, the screen shows a friendly hint."""
    ctx_with = _context_with_one_pending_file(tmp_path, tmp_path / "ignored")
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
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, BackupScreen)
        empty = app.screen.query_one("#backup-empty-state")
        assert "No backup store" in str(empty.content)


async def test_pilot_b_is_idempotent_does_not_stack_screens(tmp_path):
    """Pressing 'b' while already on BackupScreen must not push another."""
    ctx = _context_with_one_pending_file(tmp_path, tmp_path / "store")
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        depth = len(app.screen_stack)
        await pilot.press("b")
        await pilot.pause()
        assert len(app.screen_stack) == depth
