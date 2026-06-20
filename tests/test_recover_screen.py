"""Recover TUI — minimal pilot smoke (FR-5.3, Phase 6).

The orchestrator already has deep coverage in test_recovery_run.py
and the CLI surface in test_recovery_cli.py. This file locks the
two TUI-specific invariants Joan asked for:

  * The screen reuses ``execute_recovery`` directly (the engine path
    is shared with the CLI), and the artifact paths surface in the
    result modal — the recovery equivalent of restore's undo banner.
  * Per-entry status column updates after the run so a second run
    shows the prior outcome at a glance.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext, LazyServerApp
from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.run import backup_pending
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.runner import RunResult
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.loader import load_folders
from lazyserver.tconf.resolve import resolve as resolve_entry
from lazyserver.ui.recover_screen import (
    RecoverScreen,
    _RecoveryReportModal,
    format_entry_row,
)


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _ubuntu() -> Distro:
    return Distro(
        id="ubuntu",
        pretty_name="Ubuntu 24.04 LTS",
        raw_id="ubuntu",
        raw_id_like=(),
        inferred=False,
    )


def _fake_runner():
    def fake_run(argv, **kwargs):
        a = tuple(argv)
        return RunResult(
            argv=a, exit_code=0, stdout="", stderr="",
            duration_s=0.1, dry_run=False,
        )
    return fake_run


def _ctx_with_one_service(tmp_path: Path) -> tuple[AppContext, Path]:
    src = tmp_path / "smoke.conf"
    src.write_text("backed-up", encoding="utf-8")
    body = f"""
schema_version: 1
id: smoke
name: Smoke
kind: service
description: x
files:
  - id: conf
    path: {src}
    description: smoke
distros:
  ubuntu:
    package: smoke
    service_unit: smoke
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "smoke.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])

    store_path = tmp_path / "store"
    store_path.mkdir()
    store = PlainBackupStore(root=store_path, target_user=_self_user())
    baselines = BaselineStore.load(store_path, target_user=_self_user())
    resolved = [resolve_entry(e, "ubuntu", target_user=_self_user())
                for e in report.entries.values()]
    backup_pending(
        entries=resolved, store=store, baselines=baselines,
        timestamp="20260101-000000",
    )
    src.write_text("vendor-stock", encoding="utf-8")

    ctx = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=str(store_path)),
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )
    return ctx, store_path


# ---------- pure helper ----------


def test_format_entry_row_with_no_status_omits_glyph():
    """Before any run, the status column is empty — no glyph noise."""
    row = format_entry_row("bind9", "service", None)
    assert "bind9" in row
    assert "service" in row
    assert "OK" not in row
    assert "FAIL" not in row


def test_format_entry_row_renders_status_glyph_after_run():
    row = format_entry_row("bind9", "service", "ok")
    assert "✓ OK" in row


# ---------- pilot: opens, runs, modal surfaces artifact paths ----------


async def test_R_runs_all_and_modal_shows_artifact_paths(tmp_path, monkeypatch):
    """Press R from home → recover screen → R again to recover all
    → modal lands with the log + json paths shown (the recovery
    analogue of restore's undo banner). Verifies the CLI/TUI engine
    parity (artifacts actually written, visible to the operator)."""
    ctx, store_path = _ctx_with_one_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _fake_runner())
    monkeypatch.setattr(
        "lazyserver.ui.recover_screen.current_timestamp",
        lambda: "20260620-153045",
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Global R opens the recover screen.
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, RecoverScreen)
        # R inside the screen recovers all.
        await pilot.press("R")
        await pilot.pause()
        # The modal lands on top.
        assert isinstance(app.screen, _RecoveryReportModal)

        # Artifact paths land on disk (same writer as CLI).
        log_path = store_path / "recovery" / "recovery-20260620-153045.log"
        json_path = store_path / "recovery" / "recovery-20260620-153045.json"
        assert log_path.is_file()
        assert json_path.is_file()

        # And the modal surfaces both paths so the operator can cat them.
        rendered = " ".join(
            str(getattr(w, "content", w.render()))
            for w in app.screen.query("Static")
        )
        assert str(log_path) in rendered
        assert str(json_path) in rendered

        # Live file was rewritten by the engine — proves execute_recovery
        # really ran, not just the modal scaffolding.
        assert (tmp_path / "smoke.conf").read_text() == "backed-up"


async def test_r_recovers_focused_entry_only(tmp_path, monkeypatch):
    """Lowercase r recovers only the focused entry. Single-entry plan
    has one entry; the artifact JSON proves the scope was narrow."""
    import json

    ctx, store_path = _ctx_with_one_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _fake_runner())
    monkeypatch.setattr(
        "lazyserver.ui.recover_screen.current_timestamp",
        lambda: "20260620-153045",
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")  # open screen
        await pilot.pause()
        await pilot.press("r")  # recover focused only
        await pilot.pause()
        assert isinstance(app.screen, _RecoveryReportModal)

        json_path = store_path / "recovery" / "recovery-20260620-153045.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert [e["id"] for e in data["entries"]] == ["smoke"]


async def test_status_column_updates_after_run(tmp_path, monkeypatch):
    """After a run the list-view row shows the entry's outcome glyph.
    Re-pressing r/R sees the prior status at a glance."""
    ctx, store_path = _ctx_with_one_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _fake_runner())
    monkeypatch.setattr(
        "lazyserver.ui.recover_screen.current_timestamp",
        lambda: "20260620-153045",
    )

    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RecoverScreen)
        view = screen.query_one("#recover-list")
        row = list(view.children)[0]
        # No status glyph before run.
        assert "OK" not in str(row._label.content)
        await pilot.press("R")
        await pilot.pause()
        # Close the modal that just landed, return to recover screen.
        await pilot.press("escape")
        await pilot.pause()
        # Status glyph now visible on the row.
        assert "✓ OK" in str(row._label.content)


async def test_recover_screen_shows_no_store_message_when_unconfigured(tmp_path):
    """Without a backup store configured the screen shows a clean
    alert instead of crashing on the first r/R press."""
    ctx = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=None),
        distro=_ubuntu(),
        entries=(),
        shadowed=(),
    )
    app = LazyServerApp(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, RecoverScreen)
        empty = app.screen.query_one("#recover-empty-state")
        assert "No backup store" in str(empty.content)
