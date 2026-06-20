"""`lsrv recover --all` end-to-end via cmd_recover (FR-5.3, Phase 6).

Mirrors test_restore_cli: monkeypatch bootstrap() so the CLI can run
against a tmp_path-backed AppContext without touching /etc; monkeypatch
``services.control.run`` so install / enable outcomes are deterministic.

The properties under test:

  * The two artifacts land under ``<store>/recovery/`` with the right
    contents and the right ownership (write_owned does the chown).
  * The human log is printed to stdout *and* the same bytes are
    written to disk (single source of truth).
  * Exit codes match the design: 0 ok / 1 hard error / 2 partial.
  * Dry-run writes both artifacts with ``dry_run: true`` in the JSON
    and ``[DRY RUN]`` in the log header, and exits 0.
  * The `--all` requirement is enforced with a clean message, not a
    traceback.
"""

from __future__ import annotations

import io
import json
import os
import pwd
from pathlib import Path

import pytest

from lazyserver.app import AppContext
from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.run import backup_pending
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.config import Settings
from lazyserver.platform.distro import Distro
from lazyserver.platform.runner import RunResult
from lazyserver.platform.user import TargetUser
from lazyserver.recovery import cli as recovery_cli
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


def _make_runner(**overrides):
    install_exit = overrides.get("install_exit", 0)
    install_stderr = overrides.get("install_stderr", "")
    enable_exit = overrides.get("enable_exit", 0)
    enable_stderr = overrides.get("enable_stderr", "")

    def fake_run(argv, **kwargs):
        a = tuple(argv)
        if a[0] in ("apt-get", "pacman", "dnf", "zypper"):
            return RunResult(
                argv=a, exit_code=install_exit, stdout="",
                stderr=install_stderr, duration_s=1.0, dry_run=False,
            )
        if a[0] == "systemctl":
            return RunResult(
                argv=a, exit_code=enable_exit, stdout="",
                stderr=enable_stderr, duration_s=0.1, dry_run=False,
            )
        raise RuntimeError(f"unexpected argv: {a}")

    return fake_run


def _ctx_with_one_service(tmp_path: Path, store_path: Path) -> AppContext:
    src = tmp_path / "smoke.conf"
    src.write_text("backed-up-content", encoding="utf-8")
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
    settings = Settings(backup_store=str(store_path))
    return AppContext(
        target_user=_self_user(),
        settings=settings,
        distro=_ubuntu(),
        entries=tuple(report.entries.values()),
        shadowed=(),
    )


def _seed_store_with_backup(tmp_path: Path, store_path: Path, ctx: AppContext):
    store_path.mkdir()
    store = PlainBackupStore(root=store_path, target_user=_self_user())
    baselines = BaselineStore.load(store_path, target_user=_self_user())
    resolved = [resolve_entry(e, "ubuntu", target_user=_self_user()) for e in ctx.entries]
    backup_pending(
        entries=resolved, store=store, baselines=baselines,
        timestamp="20260101-000000",
    )
    # Live file edited after backup — recovery's restore must put the
    # backed-up content back.
    (tmp_path / "smoke.conf").write_text("vendor-stock", encoding="utf-8")


# ---------- happy path ----------


def test_recover_all_writes_both_artifacts_and_exits_ok(tmp_path, monkeypatch):
    """End-to-end: bootstrap → plan → run → write artifacts → exit 0."""
    store_path = tmp_path / "store"
    ctx = _ctx_with_one_service(tmp_path, store_path)
    _seed_store_with_backup(tmp_path, store_path, ctx)
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())
    monkeypatch.setattr(
        "lazyserver.recovery.cli.current_timestamp", lambda: "20260620-153045"
    )

    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=None, dry_run=False, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_OK

    # Artifacts land under <store>/recovery/.
    log_path = store_path / "recovery" / "recovery-20260620-153045.log"
    json_path = store_path / "recovery" / "recovery-20260620-153045.json"
    assert log_path.is_file()
    assert json_path.is_file()

    # Stdout contains the same human log that landed on disk.
    out_text = out.getvalue()
    log_bytes = log_path.read_text(encoding="utf-8")
    assert log_bytes in out_text
    # Artifact paths echoed to stdout so the operator finds them later.
    assert str(log_path) in out_text
    assert str(json_path) in out_text

    # JSON parses and has the schema-v1 top-level shape.
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed["timestamp"] == "20260620-153045"
    assert parsed["distro_id"] == "ubuntu"
    assert parsed["dry_run"] is False
    assert parsed["entries"][0]["id"] == "smoke"
    assert parsed["entries"][0]["status"] == "ok"

    # Live file was rewritten back to the user's backup.
    assert (tmp_path / "smoke.conf").read_text() == "backed-up-content"


# ---------- exit codes ----------


def test_install_fail_exits_partial_failure(tmp_path, monkeypatch):
    """One entry's install fails → entry status `failed` → exit 2.
    Mirrors backup/restore CLI exit semantics so a recovery script
    can branch on rc == 2."""
    store_path = tmp_path / "store"
    ctx = _ctx_with_one_service(tmp_path, store_path)
    _seed_store_with_backup(tmp_path, store_path, ctx)
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx)
    monkeypatch.setattr(
        "lazyserver.services.control.run",
        _make_runner(install_exit=100, install_stderr="E: locate failed"),
    )

    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=None, dry_run=False, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_PARTIAL_FAILURE


def test_missing_all_flag_returns_hard_error_with_clean_message(tmp_path, monkeypatch):
    """`recover` without `--all` is not a usage error (argparse takes
    it as falsy) — we surface a clear hint instead of running with
    empty intent."""
    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=False, store_override=None, dry_run=False, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_HARD_ERROR
    assert "--all" in err.getvalue()
    assert "restore --entry" in err.getvalue()


def test_no_store_configured_returns_hard_error(tmp_path, monkeypatch):
    """Missing backup_store is a hard precondition fail — exit 1."""
    ctx_no_store = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=None),
        distro=_ubuntu(),
        entries=(),
        shadowed=(),
    )
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx_no_store)
    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=None, dry_run=False, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_HARD_ERROR
    assert "no backup store" in err.getvalue().lower()


def test_no_entries_resolved_returns_hard_error(tmp_path, monkeypatch):
    """Distro mismatch / empty tconf → nothing to do → exit 1, not 0."""
    store_path = tmp_path / "store"
    store_path.mkdir()
    ctx = AppContext(
        target_user=_self_user(),
        settings=Settings(backup_store=str(store_path)),
        distro=_ubuntu(),
        entries=(),
        shadowed=(),
    )
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx)
    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=None, dry_run=False, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_HARD_ERROR
    assert "nothing to recover" in err.getvalue().lower()


# ---------- dry-run ----------


def test_dry_run_writes_artifacts_with_dry_run_flag(tmp_path, monkeypatch):
    """Dry-run still produces both artifacts (Joan's decision 7) and
    exits 0 — the plan is a useful script-readable preview."""
    store_path = tmp_path / "store"
    ctx = _ctx_with_one_service(tmp_path, store_path)
    _seed_store_with_backup(tmp_path, store_path, ctx)
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx)

    # Defensive: dry-run must not touch the system. If services.control.run
    # is ever called we want the test to fail loudly.
    def boom(argv, **kwargs):
        raise AssertionError(f"dry-run must not call subprocess: {argv}")

    monkeypatch.setattr("lazyserver.services.control.run", boom)
    monkeypatch.setattr(
        "lazyserver.recovery.cli.current_timestamp", lambda: "20260620-153045"
    )

    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=None, dry_run=True, out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_OK

    log_path = store_path / "recovery" / "recovery-20260620-153045.log"
    json_path = store_path / "recovery" / "recovery-20260620-153045.json"

    log_text = log_path.read_text(encoding="utf-8")
    assert "[DRY RUN]" in log_text
    assert "WOULD-RUN" in log_text

    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["dry_run"] is True
    assert parsed["entries"][0]["status"] == "would_run"

    # Live file untouched.
    assert (tmp_path / "smoke.conf").read_text() == "vendor-stock"


# ---------- override ----------


def test_store_override_takes_precedence_over_settings(tmp_path, monkeypatch):
    """--store wins over settings.backup_store; artifacts land at the
    override path, not the configured one."""
    store_path = tmp_path / "override-store"
    ctx = _ctx_with_one_service(tmp_path, tmp_path / "settings-store")
    _seed_store_with_backup(tmp_path, store_path, ctx)
    monkeypatch.setattr(recovery_cli, "bootstrap", lambda: ctx)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())
    monkeypatch.setattr(
        "lazyserver.recovery.cli.current_timestamp", lambda: "20260620-153045"
    )

    out, err = io.StringIO(), io.StringIO()
    rc = recovery_cli.cmd_recover(
        all_entries=True, store_override=store_path, dry_run=False,
        out=out, err=err,
    )
    assert rc == recovery_cli.EXIT_OK
    # Artifact path is under the OVERRIDE, not the settings store.
    assert (store_path / "recovery" / "recovery-20260620-153045.log").is_file()
    assert not (tmp_path / "settings-store" / "recovery").exists()
