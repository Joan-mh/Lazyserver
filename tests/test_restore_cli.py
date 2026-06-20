"""`lsrv restore` end-to-end via cmd_restore.

Mirrors the test_backup_cli posture: monkeypatch bootstrap() to inject
a synthetic AppContext so the CLI can be driven against tmp_path files
without any real /etc state. The restore-specific properties we care
about here are:

  * The FR-3.2 safety contract is wired all the way to the CLI (pre-
    restore snapshot exists and is surfaced as the undo handle).
  * --snapshot pins, --all + --snapshot is rejected, unknown entries
    are hard errors, and --file finds its owning entry.
  * Extras (FR-3.4) and dangerous-mode warnings (decision (d)) make it
    into the human output rather than getting buried.
  * Exit codes 0/1/2 line up so Phase 6 recovery can branch on them.
"""

from __future__ import annotations

import io
import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext
from lazyserver.backup import restore_cli
from lazyserver.backup.restore import PRE_RESTORE_SUFFIX, RestoreOutcome
from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.run import backup_pending
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.loader import load_folders
from lazyserver.tconf.resolve import resolve as resolve_entry


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


def _context_with_one_entry(tmp_path: Path, store_path: Path) -> AppContext:
    body = f"""
schema_version: 1
id: smoke
name: Smoke
kind: service
description: x
files:
  - id: conf
    path: {tmp_path / 'smoke.conf'}
    description: smoke conf
distros:
  ubuntu:
    package: smoke
    service_unit: smoke
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "smoke.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])

    settings = Settings(backup_store=str(store_path))
    return AppContext(
        target_user=_self_user(),
        settings=settings,
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )


def _context_with_file_set(tmp_path: Path, store_path: Path) -> AppContext:
    set_dir = tmp_path / "zones"
    set_dir.mkdir()
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

    settings = Settings(backup_store=str(store_path))
    return AppContext(
        target_user=_self_user(),
        settings=settings,
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )


def _seed_backup(ctx: AppContext, store_path: Path, *, timestamp: str = "t1") -> None:
    """Run one backup pass so the store has a snapshot to restore from."""
    resolved = [
        resolve_entry(e, ctx.distro.id, target_user=ctx.target_user)
        for e in ctx.entries
    ]
    store_path.mkdir(parents=True, exist_ok=True)
    store = PlainBackupStore(root=store_path, target_user=ctx.target_user)
    baselines = BaselineStore.load(store_path, target_user=ctx.target_user)
    backup_pending(
        entries=resolved,
        store=store,
        baselines=baselines,
        timestamp=timestamp,
    )


@pytest.fixture
def fake_context(tmp_path: Path, monkeypatch):
    store_path = tmp_path / "store"
    ctx = _context_with_one_entry(tmp_path, store_path)
    monkeypatch.setattr(restore_cli, "bootstrap", lambda: ctx)
    return ctx, store_path


@pytest.fixture
def fake_set_context(tmp_path: Path, monkeypatch):
    store_path = tmp_path / "store"
    ctx = _context_with_file_set(tmp_path, store_path)
    monkeypatch.setattr(restore_cli, "bootstrap", lambda: ctx)
    return ctx, store_path


# ---------- happy path: --entry ----------


def test_entry_mode_restores_and_prints_pre_restore_ts(fake_context, tmp_path, monkeypatch):
    """The whole point of Phase 5 from the CLI: --entry restores files,
    captures a pre-restore snapshot, and surfaces the undo timestamp."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("edited-after-backup", encoding="utf-8")

    # Pin the pre-restore timestamp so the assertion is deterministic.
    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "20260620-153045")

    out, err = io.StringIO(), io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=out, err=err,
    )

    text = out.getvalue()
    assert code == restore_cli.EXIT_OK, err.getvalue()
    # Restored content.
    assert src.read_text() == "v1"
    # Pre-restore snapshot landed in the store with the printed timestamp.
    pre_ts = f"20260620-153045{PRE_RESTORE_SUFFIX}"
    pre_path = store_path / "smoke" / pre_ts / Path(*src.parts[1:])
    assert pre_path.exists(), "pre-restore snapshot must be on disk"
    assert pre_path.read_text() == "edited-after-backup"
    # Prominent in the output, with the exact undo command.
    assert f"Pre-restore snapshot: {pre_ts}" in text
    assert f"lsrv restore --entry smoke --snapshot {pre_ts}" in text


def test_entry_mode_with_explicit_snapshot_pins(fake_context, tmp_path, monkeypatch):
    """--snapshot TS picks that specific timestamp, not the latest."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("v2", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t2")
    # Live edit after both backups.
    src.write_text("edited", encoding="utf-8")

    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "TS")
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot="t1",
        store_override=None, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )

    assert code == restore_cli.EXIT_OK
    # We asked for t1, so live file rewinds to v1, not v2.
    assert src.read_text() == "v1"


def test_entry_mode_unknown_id_is_hard_error(fake_context):
    """Unknown entry id is a clean hard error, not a traceback."""
    err = io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="does-not-exist", all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )
    assert code == restore_cli.EXIT_HARD_ERROR
    assert "does-not-exist" in err.getvalue()


def test_entry_mode_missing_snapshot_is_hard_error(fake_context, tmp_path):
    """User asked for a timestamp that doesn't exist → don't silently
    slide to latest. The planner reports it as missing; with nothing to
    restore that's a hard error."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")

    out, err = io.StringIO(), io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot="never-existed",
        store_override=None, dry_run=False, out=out, err=err,
    )
    assert code == restore_cli.EXIT_HARD_ERROR
    assert "smoke" in (out.getvalue() + err.getvalue())


# ---------- --file mode ----------


def test_file_mode_restores_one_file(fake_context, tmp_path, monkeypatch):
    """--file PATH finds the owning entry and restores only that path."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("edited", encoding="utf-8")

    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "TS")
    out = io.StringIO()
    code = restore_cli.cmd_restore(
        file=src, entry=None, all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=out, err=io.StringIO(),
    )
    assert code == restore_cli.EXIT_OK
    assert src.read_text() == "v1"
    assert "Pre-restore snapshot" in out.getvalue()


def test_file_mode_not_owned_is_hard_error(fake_context, tmp_path):
    """A file outside any entry's tconf definition shouldn't be silently
    restored from some other entry's snapshot."""
    ctx, _ = fake_context
    err = io.StringIO()
    stray = tmp_path / "not-managed.conf"
    stray.write_text("x")
    code = restore_cli.cmd_restore(
        file=stray, entry=None, all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )
    assert code == restore_cli.EXIT_HARD_ERROR
    assert "not managed" in err.getvalue() or "tconf" in err.getvalue()


# ---------- --all mode ----------


def test_all_mode_restores_every_entry(fake_context, tmp_path, monkeypatch):
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("edited", encoding="utf-8")

    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "TS")
    code = restore_cli.cmd_restore(
        file=None, entry=None, all_entries=True, snapshot=None,
        store_override=None, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )
    assert code == restore_cli.EXIT_OK
    assert src.read_text() == "v1"


def test_all_with_snapshot_is_rejected(fake_context):
    """--all + --snapshot is rejected at the CLI layer — it would
    require the same TS to exist for every entry, which is rarely
    true; the user should use --entry ID --snapshot TS instead."""
    err = io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry=None, all_entries=True, snapshot="t1",
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )
    assert code == restore_cli.EXIT_HARD_ERROR
    assert "--snapshot" in err.getvalue()
    assert "--all" in err.getvalue()


# ---------- --dry-run ----------


def test_dry_run_prints_plan_and_writes_nothing(fake_context, tmp_path, monkeypatch):
    """Dry-run prints what WOULD happen, including the pre-restore TS
    that would be captured, but leaves the live file and store alone."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("edited", encoding="utf-8")

    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "20260620-153045")
    out = io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot=None,
        store_override=None, dry_run=True, out=out, err=io.StringIO(),
    )
    assert code == restore_cli.EXIT_OK
    text = out.getvalue()
    assert "OVERWRITE" in text
    assert "would be restored" in text
    assert f"20260620-153045{PRE_RESTORE_SUFFIX}" in text
    # Live file untouched.
    assert src.read_text() == "edited"
    # No pre-restore dir.
    assert not (store_path / "smoke" / f"20260620-153045{PRE_RESTORE_SUFFIX}").exists()


# ---------- FR-3.4 extras surface ----------


def test_extras_shown_in_output_and_not_deleted(fake_set_context, tmp_path):
    """FR-3.4: a live file_set member absent from the snapshot is
    reported in the output and left in place — the user can see what
    won't be touched and decide separately."""
    ctx, store_path = fake_set_context
    set_dir = tmp_path / "zones"
    a = set_dir / "db.example"
    a.write_text("A")
    _seed_backup(ctx, store_path, timestamp="t1")

    # Add an extra after backup.
    extra = set_dir / "db.added-later"
    extra.write_text("E")

    out = io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="zoned", all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=out, err=io.StringIO(),
    )
    assert code == restore_cli.EXIT_OK
    text = out.getvalue()
    assert str(extra) in text
    assert "extra" in text.lower()
    # The extra is still on disk.
    assert extra.exists()
    assert extra.read_text() == "E"


# ---------- store / config resolution ----------


def test_missing_store_is_hard_error(tmp_path, monkeypatch):
    ctx = _context_with_one_entry(tmp_path, store_path=tmp_path / "unused")
    ctx = AppContext(
        target_user=ctx.target_user,
        settings=Settings(backup_store=None),
        distro=ctx.distro,
        entries=ctx.entries,
        shadowed=(),
    )
    monkeypatch.setattr(restore_cli, "bootstrap", lambda: ctx)
    err = io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )
    assert code == restore_cli.EXIT_HARD_ERROR
    assert "--store" in err.getvalue()
    assert "config.toml" in err.getvalue()


def test_store_override_takes_precedence(fake_context, tmp_path, monkeypatch):
    """--store wins over settings.backup_store."""
    ctx, default_store = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    override = tmp_path / "override-store"

    # Seed the override store, not the default.
    _seed_backup(ctx, override, timestamp="t1")
    src.write_text("edited", encoding="utf-8")

    monkeypatch.setattr(restore_cli, "current_timestamp", lambda: "TS")
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot=None,
        store_override=override, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )
    assert code == restore_cli.EXIT_OK
    assert src.read_text() == "v1"
    # Default store is untouched.
    assert not default_store.exists()


# ---------- partial failure exit code ----------


def test_partial_failure_returns_exit_2(fake_context, tmp_path, monkeypatch):
    """Pre-snapshot failure on the only item — exit 2, live file untouched."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")
    src.write_text("edited", encoding="utf-8")

    real_make_store = restore_cli.make_backup_store

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

    monkeypatch.setattr(
        restore_cli, "make_backup_store",
        lambda root, target_user: _SnapshotKiller(real_make_store(root, target_user)),
    )

    out, err = io.StringIO(), io.StringIO()
    code = restore_cli.cmd_restore(
        file=None, entry="smoke", all_entries=False, snapshot=None,
        store_override=None, dry_run=False, out=out, err=err,
    )

    assert code == restore_cli.EXIT_PARTIAL_FAILURE
    assert "✗" in out.getvalue()
    assert "synthetic" in out.getvalue()
    # Critical: live file untouched (FR-3.2 + NFR-2).
    assert src.read_text() == "edited"


# ---------- top-level argparse wiring ----------


def test_top_level_cli_dispatches_to_restore(fake_context, tmp_path):
    """`cli.main(['restore', '--entry', 'smoke'])` reaches cmd_restore."""
    from lazyserver import cli as top_cli

    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    _seed_backup(ctx, store_path, timestamp="t1")

    # --dry-run is the global flag and lives before the subcommand.
    code = top_cli.main(["--dry-run", "restore", "--entry", "smoke"])
    assert code == restore_cli.EXIT_OK


def test_top_level_cli_requires_a_mode(capsys):
    """`lsrv restore` with no mode flag must argparse-fail loudly."""
    from lazyserver import cli as top_cli

    with pytest.raises(SystemExit) as excinfo:
        top_cli.main(["restore"])
    assert excinfo.value.code != 0
    assert "required" in capsys.readouterr().err.lower()


def test_top_level_cli_restore_help_lists_modes(capsys):
    """The help text exposes --file/--entry/--all to the user."""
    from lazyserver import cli as top_cli

    with pytest.raises(SystemExit):
        top_cli.main(["restore", "--help"])
    text = capsys.readouterr().out
    assert "--file" in text
    assert "--entry" in text
    assert "--all" in text
    assert "--snapshot" in text
