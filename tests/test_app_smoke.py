"""Phase 2 smoke tests: the TUI launches, lists entries, and navigates.

Uses Textual's `App.run_test()` to drive the app headlessly. The goal is
not to pixel-test the rendering but to confirm the screens compose without
exceptions, expose the right entries, and react to keys.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext, LazyServerApp
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.user import TargetUser
from lazyserver.tconf import bundled_tconf_path, loader


def _self_target_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _arch_context() -> AppContext:
    entries = tuple(loader.load_folder(bundled_tconf_path()))
    return AppContext(
        target_user=_self_target_user(),
        settings=Settings(),
        distro=Distro(
            id="arch",
            pretty_name="Arch Linux",
            raw_id="arch",
            raw_id_like=(),
            inferred=False,
        ),
        entries=entries,
        shadowed=(),
    )


def _inferred_context() -> AppContext:
    ctx = _arch_context()
    inferred = Distro(
        id="arch",
        pretty_name="Garuda Linux",
        raw_id="garuda",
        raw_id_like=("arch",),
        inferred=True,
    )
    return AppContext(
        target_user=ctx.target_user,
        settings=ctx.settings,
        distro=inferred,
        entries=ctx.entries,
        shadowed=ctx.shadowed,
    )


async def test_home_screen_shows_services_and_apps_sections():
    ctx = _arch_context()
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Section labels render.
        section_titles = [s.content for s in app.screen.query(".section-title")]
        joined = " ".join(str(t) for t in section_titles)
        assert "Services" in joined
        assert "Apps" in joined
        # All shipped service ids are in the services list.
        from lazyserver.ui.home_screen import _EntryItem
        services_list = app.screen.query_one("#services-list")
        service_ids = {item.entry.id for item in services_list.query(_EntryItem)}
        assert {"bind9", "nginx", "postfix", "dovecot"}.issubset(service_ids)
        apps_list = app.screen.query_one("#apps-list")
        app_ids = {item.entry.id for item in apps_list.query(_EntryItem)}
        assert "neovim" in app_ids


async def test_entry_screen_shows_resolved_paths_and_alias_note():
    ctx = _arch_context()
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Navigate: focus services list (it's the first one), highlight bind9, Enter.
        from lazyserver.ui.home_screen import _EntryItem
        services_list = app.screen.query_one("#services-list")
        bind9_item = next(
            item for item in services_list.query(_EntryItem)
            if item.entry.id == "bind9"
        )
        services_list.index = services_list._nodes.index(bind9_item)
        await pilot.press("enter")
        await pilot.pause()
        # We're on the entry screen now.
        from lazyserver.ui.entry_screen import EntryScreen
        assert isinstance(app.screen, EntryScreen)
        # The alias note for bind9-on-arch is rendered.
        notes = [str(s.content) for s in app.screen.query(".alias-note")]
        assert any("/etc/named.conf" in n for n in notes), notes
        # And the resolved files appear in the files list.
        from lazyserver.ui.entry_screen import _FileRow
        rows = [str(r.children[0].content) for r in app.screen.query(_FileRow)]
        joined = " ".join(rows)
        assert "/etc/named.conf" in joined
        # zone_files set is shown as a (set) row.
        assert "zone_files" in joined and "(set)" in joined


async def test_file_screen_shows_description_and_example():
    ctx = _arch_context()
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        from lazyserver.ui.home_screen import _EntryItem
        services_list = app.screen.query_one("#services-list")
        nginx_item = next(
            item for item in services_list.query(_EntryItem)
            if item.entry.id == "nginx"
        )
        services_list.index = services_list._nodes.index(nginx_item)
        await pilot.press("enter")
        await pilot.pause()
        # Now on entry screen. Pick the first file.
        from lazyserver.ui.entry_screen import _FileRow
        files_list = app.screen.query_one("#files-list")
        first_row = next(iter(files_list.query(_FileRow)))
        files_list.index = files_list._nodes.index(first_row)
        await pilot.press("enter")
        await pilot.pause()
        from lazyserver.ui.file_screen import FileScreen
        assert isinstance(app.screen, FileScreen)
        # Description and example sections render.
        examples = [str(s.content) for s in app.screen.query(".example")]
        assert examples, "FileScreen should render an .example block"


async def test_inferred_distro_notice_appears_in_status_line():
    ctx = _inferred_context()
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.screen.query_one("#status-line").content)
        assert "Garuda" in status
        assert "arch" in status
        assert "⚠" in status


async def test_dry_run_flag_shows_indicator_in_status_line():
    ctx = _arch_context()
    app = LazyServerApp(ctx, dry_run=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.screen.query_one("#status-line").content
        assert "DRY-RUN" in status


async def test_d_key_toggles_dry_run():
    ctx = _arch_context()
    app = LazyServerApp(ctx)  # starts with dry_run=False
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.screen.query_one("#status-line").content
        assert "DRY-RUN" not in before
        await pilot.press("d")
        await pilot.pause()
        assert app.dry_run is True
        after = app.screen.query_one("#status-line").content
        assert "DRY-RUN" in after
        await pilot.press("d")
        await pilot.pause()
        assert app.dry_run is False


async def test_escape_pops_to_home():
    ctx = _arch_context()
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        from lazyserver.ui.home_screen import _EntryItem, HomeScreen
        services_list = app.screen.query_one("#services-list")
        first = next(iter(services_list.query(_EntryItem)))
        services_list.index = services_list._nodes.index(first)
        await pilot.press("enter")
        await pilot.pause()
        from lazyserver.ui.entry_screen import EntryScreen
        assert isinstance(app.screen, EntryScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
