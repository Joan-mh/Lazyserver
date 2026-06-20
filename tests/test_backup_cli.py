"""`lsrv backup` end-to-end via cmd_backup.

We bypass argparse and call cmd_backup directly so we can monkeypatch
bootstrap() to inject a synthetic AppContext — exactly what Phase 6
recovery will need to drive backup from inside another flow without
re-parsing argv.
"""

from __future__ import annotations

import io
import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext
from lazyserver.backup import cli as backup_cli
from lazyserver.backup.pending import BASELINES_FILENAME, BaselineStore
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.user import TargetUser
from lazyserver.tconf import bundled_tconf_path
from lazyserver.tconf.loader import load_folders


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
    """Synthesise an AppContext around one fake entry whose only managed
    file lives under tmp_path, so we never touch /etc/* during tests.
    """
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


@pytest.fixture
def fake_context(tmp_path: Path, monkeypatch):
    """Patch bootstrap() to return the test context. Returns (ctx, store_path)."""
    store_path = tmp_path / "store"
    ctx = _context_with_one_entry(tmp_path, store_path)
    monkeypatch.setattr(backup_cli, "bootstrap", lambda: ctx)
    return ctx, store_path


# ---------- mode plumbing ----------


def test_list_mode_shows_pending_without_writing(fake_context, tmp_path):
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    out = io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=True, all_pending=False, entry_ids=None,
        store_override=None, dry_run=False, out=out, err=io.StringIO(),
    )

    assert code == backup_cli.EXIT_OK
    text = out.getvalue()
    assert "smoke" in text
    assert "NEW" in text
    assert "pending" in text  # the "pending" verb in the summary line
    # And nothing was written.
    assert not (store_path / BASELINES_FILENAME).exists()


def test_all_mode_writes_snapshots_and_baselines(fake_context, tmp_path):
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False, out=out, err=err,
    )

    assert code == backup_cli.EXIT_OK, err.getvalue()
    assert "Backed up 1 file(s)" in out.getvalue()
    # Baselines persisted.
    assert (store_path / BASELINES_FILENAME).exists()
    # Snapshot file landed under <store>/<entry>/<ts>/<source-rel>.
    snaps = list((store_path / "smoke").iterdir())
    assert len(snaps) == 1
    stored = snaps[0] / Path(*src.parts[1:])
    assert stored.is_file()
    assert stored.read_text() == "v1"


def test_entry_mode_only_backs_up_selected(fake_context, tmp_path):
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=False, entry_ids=["smoke"],
        store_override=None, dry_run=False, out=out, err=err,
    )

    assert code == backup_cli.EXIT_OK
    assert "smoke" in out.getvalue()
    assert (store_path / "smoke").is_dir()


def test_unknown_entry_id_is_hard_error(fake_context):
    ctx, _ = fake_context
    err = io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=False, entry_ids=["does-not-exist"],
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )

    assert code == backup_cli.EXIT_HARD_ERROR
    assert "does-not-exist" in err.getvalue()


# ---------- dry-run ----------


def test_dry_run_does_not_write_even_with_all(fake_context, tmp_path):
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    out = io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=True, out=out, err=io.StringIO(),
    )

    assert code == backup_cli.EXIT_OK
    assert "would be backed up" in out.getvalue()
    assert not (store_path / BASELINES_FILENAME).exists()
    assert not (store_path / "smoke").exists()


# ---------- config / store resolution ----------


def test_missing_store_is_hard_error(tmp_path, monkeypatch):
    """No backup_store in settings, no --store override → fail with a
    clear message pointing at both fix paths."""
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    ctx = _context_with_one_entry(tmp_path, store_path=tmp_path / "unused")
    # Strip the store so resolution must fail.
    ctx = AppContext(
        target_user=ctx.target_user,
        settings=Settings(backup_store=None),
        distro=ctx.distro,
        entries=ctx.entries,
        shadowed=(),
    )
    monkeypatch.setattr(backup_cli, "bootstrap", lambda: ctx)
    err = io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=True, all_pending=False, entry_ids=None,
        store_override=None, dry_run=False, out=io.StringIO(), err=err,
    )

    assert code == backup_cli.EXIT_HARD_ERROR
    assert "--store" in err.getvalue()
    assert "config.toml" in err.getvalue()


def test_first_run_announces_backup_store_creation(fake_context, tmp_path):
    """If the store dir didn't exist, surface a prominent notice with the
    resolved absolute path — the diagnostic that makes a ~-misexpansion
    or a typo visible."""
    ctx, store_path = fake_context
    assert not store_path.exists()
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    out = io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False, out=out, err=io.StringIO(),
    )

    assert code == backup_cli.EXIT_OK
    assert f"Created backup store at {store_path}" in out.getvalue()


def test_auto_created_store_root_is_owned_by_target_user(
    fake_context, tmp_path, monkeypatch
):
    """End-to-end: a store root LazyServer creates under sudo must end up
    owned by the target user, not root. Tests run as a normal user so
    we spy on the underlying chown call to capture the uid/gid the
    backup layer asked the kernel for."""
    from lazyserver.backup import _fsutil

    ctx, store_path = fake_context
    assert not store_path.exists()
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")

    calls: list[tuple[str, int, int]] = []
    real_chown = _fsutil.os.chown

    def spy_chown(path, uid, gid, **kw):
        calls.append((str(path), uid, gid))
        return real_chown(path, uid, gid, **kw)

    monkeypatch.setattr(_fsutil.os, "chown", spy_chown)

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )

    assert code == backup_cli.EXIT_OK
    # The store root itself was chowned to the target user.
    root_calls = [c for c in calls if c[0] == str(store_path)]
    assert root_calls, "store root was never chowned"
    assert all(uid == ctx.target_user.uid for _, uid, _ in root_calls)


def test_pre_existing_store_root_is_not_chowned(
    fake_context, tmp_path, monkeypatch
):
    """Mirror of the 4b rule applied throughout the store: a root the
    student pre-created reflects their deliberate choice — LazyServer
    must not touch its ownership, only chown what it creates itself."""
    from lazyserver.backup import _fsutil

    ctx, store_path = fake_context
    store_path.mkdir(mode=0o700)  # student pre-created with weird mode
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")

    calls: list[tuple[str, int, int]] = []
    real_chown = _fsutil.os.chown

    def spy_chown(path, uid, gid, **kw):
        calls.append((str(path), uid, gid))
        return real_chown(path, uid, gid, **kw)

    monkeypatch.setattr(_fsutil.os, "chown", spy_chown)

    backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )

    assert all(c[0] != str(store_path) for c in calls), (
        f"chown was called on pre-existing store root: {calls}"
    )


def test_subsequent_run_does_not_announce_creation(fake_context, tmp_path):
    """The notice fires only on cold-create. A second run on an existing
    store must not repeat it."""
    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")

    # First run creates the store.
    backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )
    # Second run — touch the source again so something is pending.
    src.write_text("v2", encoding="utf-8")
    out = io.StringIO()
    backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False, out=out, err=io.StringIO(),
    )
    assert "Created backup store" not in out.getvalue()


def test_store_override_takes_precedence(fake_context, tmp_path):
    """--store wins over settings.backup_store."""
    ctx, settings_store = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")
    override = tmp_path / "override-store"

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=override, dry_run=False,
        out=io.StringIO(), err=io.StringIO(),
    )

    assert code == backup_cli.EXIT_OK
    assert (override / "smoke").is_dir()
    # Settings store path stays untouched.
    assert not settings_store.exists()


# ---------- partial-failure exit code ----------


def test_partial_failure_returns_exit_2(fake_context, tmp_path, monkeypatch):
    """One snapshot failure → exit 2, so Phase 6 recovery can branch on it."""
    from lazyserver.backup import run as run_mod

    ctx, store_path = fake_context
    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")

    # Make every snapshot raise.
    real_make_store = backup_cli.make_backup_store

    class _ExplodingStore:
        def __init__(self, inner):
            self.root = inner.root
            self._inner = inner
        def snapshot(self, **kw): raise OSError("synthetic disk full")
        def list_snapshots(self, eid): return self._inner.list_snapshots(eid)
        def list_files(self, eid, ts): return self._inner.list_files(eid, ts)
        def read(self, ref): return self._inner.read(ref)
        def commit_operation(self, *, message): return None

    monkeypatch.setattr(
        backup_cli, "make_backup_store",
        lambda root, target_user: _ExplodingStore(real_make_store(root, target_user)),
    )
    out, err = io.StringIO(), io.StringIO()

    code = backup_cli.cmd_backup(
        list_only=False, all_pending=True, entry_ids=None,
        store_override=None, dry_run=False, out=out, err=err,
    )

    assert code == backup_cli.EXIT_PARTIAL_FAILURE
    assert "✗" in out.getvalue()
    assert "synthetic disk full" in out.getvalue()
    assert "failed" in err.getvalue().lower()


# ---------- argparse wiring ----------


def test_top_level_cli_dispatches_to_backup(fake_context, tmp_path, monkeypatch):
    """Smoke test that `cli.main(['backup', '--list'])` reaches cmd_backup."""
    from lazyserver import cli as top_cli

    src = tmp_path / "smoke.conf"
    src.write_text("v1", encoding="utf-8")

    # cli.main writes to real stdout/stderr; use capsys-style capture via
    # the function we already monkeypatched (bootstrap is patched in the
    # fixture, so cmd_backup picks up our context).
    code = top_cli.main(["backup", "--list"])
    assert code == backup_cli.EXIT_OK


def test_top_level_cli_requires_a_mode(capsys):
    """`lsrv backup` with no mode flag must argparse-fail loudly."""
    from lazyserver import cli as top_cli

    with pytest.raises(SystemExit) as excinfo:
        top_cli.main(["backup"])
    assert excinfo.value.code != 0
    assert "required" in capsys.readouterr().err.lower()
