"""FR-1.6/FR-1.7 — managed-files list refreshes after state-changing actions.

Without a refresh, a student who creates a new zone file sees nothing
change and may conclude the create failed. These tests assert the
file_set view re-expands its glob:

  1. after the create-file modal returns (was the live-VM bug),
  2. after the editor returns (covers the wider class — create from
     outside LazyServer, or a save that triggers a rename(2)).
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from textual.widgets import ListView

from lazyserver.app import AppContext, LazyServerApp
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.runner import RunResult
from lazyserver.platform.user import TargetUser
from lazyserver.tconf import bundled_tconf_path, loader
from lazyserver.tconf.resolve import ResolvedFileSet


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


def _zone_set(directory: Path) -> ResolvedFileSet:
    return ResolvedFileSet(
        id="zone_files",
        directory=str(directory),
        pattern="db.*",
        description="zone files",
        example="$TTL 86400\n",
        optional=True,
        owner=None,
        group=None,
        mode=None,
    )


def _set_member_paths(screen) -> list[str]:
    try:
        view = screen.query_one("#set-members", ListView)
    except Exception:
        return []
    paths = []
    for child in view.children:
        # _SetMemberRow exposes .path; fall back to the label otherwise.
        path = getattr(child, "path", None)
        if path is not None:
            paths.append(str(path))
    return paths


async def test_create_refreshes_set_members_without_restart(tmp_path, monkeypatch):
    """The live-VM bug: after creating db.foo.lan in the zone_files set,
    it must appear in #set-members immediately."""
    ctx = _arch_context()
    set_dir = tmp_path / "zones"
    set_dir.mkdir()

    fs = _zone_set(set_dir)

    from lazyserver.ui import file_screen as fs_mod

    # The editor is launched right after create — make it a no-op so the
    # file's content (the example pre-fill) is not what we're testing.
    def fake_launch(settings, path, *, dry_run=False, env=None):
        return RunResult(
            argv=("fake-editor", str(path)),
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.0,
            dry_run=dry_run,
        )

    monkeypatch.setattr(fs_mod, "launch_editor", fake_launch)

    bind9 = next(e for e in ctx.entries if e.id == "bind9")
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(fs_mod.FileScreen(ctx, bind9, fs))
        await pilot.pause()
        # Empty set on entry: the placeholder (not a ListView) is rendered.
        assert _set_member_paths(app.screen) == []

        await pilot.press("n")
        await pilot.pause()
        from lazyserver.ui.create_screen import NewFileScreen
        assert isinstance(app.screen, NewFileScreen)
        app.screen.query_one("#filename-input").value = "db.example.lan"
        await pilot.press("enter")
        await pilot.pause()

        # Back on FileScreen. The set view now lists the new file —
        # *without* a restart and without reopening the screen.
        assert isinstance(app.screen, fs_mod.FileScreen)
        members = _set_member_paths(app.screen)
        assert str(set_dir / "db.example.lan") in members


async def test_edit_return_refreshes_set_members(tmp_path, monkeypatch):
    """A file created externally during the editor session must appear
    on return — the editor return is the generic refresh point."""
    ctx = _arch_context()
    set_dir = tmp_path / "zones"
    set_dir.mkdir()
    pre = set_dir / "db.preexisting"
    pre.write_text("zone\n")

    fs = _zone_set(set_dir)
    from lazyserver.ui import file_screen as fs_mod

    new_path = set_dir / "db.created-during-edit"

    def fake_launch(settings, path, *, dry_run=False, env=None):
        # Simulate the user (or another process) creating a sibling
        # file while the editor is "open".
        new_path.write_text("new\n")
        return RunResult(
            argv=("fake-editor", str(path)),
            exit_code=0,
            stdout="",
            stderr="",
            duration_s=0.0,
            dry_run=dry_run,
        )

    monkeypatch.setattr(fs_mod, "launch_editor", fake_launch)

    bind9 = next(e for e in ctx.entries if e.id == "bind9")
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(fs_mod.FileScreen(ctx, bind9, fs))
        await pilot.pause()
        # Only db.preexisting is shown initially.
        before = _set_member_paths(app.screen)
        assert before == [str(pre)]

        # Press Enter on the focused set member → editor launches.
        await pilot.press("enter")
        await pilot.pause()
        # Now the new file appears too.
        after = _set_member_paths(app.screen)
        assert str(new_path) in after
        assert str(pre) in after
