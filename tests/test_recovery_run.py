"""Recovery orchestrator — cascade + dry-run integration (FR-5.3).

The orchestrator wires the planner, the install primitive, the
restore engine and the enable action into one report-producing
function. Tests use:

  * a real ``PlainBackupStore`` (snapshots seeded by ``backup_pending``
    so the metadata.json the restore step reads is authentic);
  * a monkeypatched ``services.control.run`` so install / enable
    outcomes are deterministic without firing apt or systemctl;
  * direct construction of ``ResolvedEntry`` instances where the
    YAML+loader path is noise for the property under test.

We assert the cascade rule end-to-end: install-fail cascades to skip
restore + enable; restore-fail does *not* cascade (enable still runs);
the per-entry status rolls up correctly through
``derive_entry_status``.

The "no pre-restore snapshot during recovery" invariant (Joan's
decision 2) is enforced via the take_pre_restore=False kwarg on
``execute_restore``; we spot-check it here and exercise the kwarg's
own unit test in test_backup_restore.py.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup.pending import BaselineStore
from lazyserver.backup.restore import PRE_RESTORE_SUFFIX
from lazyserver.backup.run import backup_pending
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.runner import RunResult
from lazyserver.platform.user import TargetUser
from lazyserver.recovery.plan import build_recovery_plan
from lazyserver.recovery.report import (
    ENTRY_FAILED,
    ENTRY_OK,
    ENTRY_PARTIAL,
    ENTRY_WOULD_RUN,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_WOULD_RUN,
)
from lazyserver.recovery.run import CASCADE_REASON, execute_recovery
from lazyserver.tconf.loader import load_folders
from lazyserver.tconf.resolve import resolve as resolve_entry


# ---------- helpers ----------


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _make_runner(**overrides):
    """Build a fake `services.control.run` that branches on argv[0].

    Defaults are happy-path (exit 0). Pass `install_exit=…` /
    `install_stderr=…` / `enable_exit=…` / `enable_stderr=…` to
    simulate failures.
    """
    install_exit = overrides.get("install_exit", 0)
    install_stderr = overrides.get("install_stderr", "")
    install_duration = overrides.get("install_duration", 1.0)
    enable_exit = overrides.get("enable_exit", 0)
    enable_stderr = overrides.get("enable_stderr", "")
    enable_duration = overrides.get("enable_duration", 0.1)

    def fake_run(argv, **kwargs):
        a = tuple(argv)
        if not a:
            raise RuntimeError("empty argv passed to fake runner")
        if a[0] in ("apt-get", "pacman", "dnf", "zypper"):
            return RunResult(
                argv=a, exit_code=install_exit, stdout="",
                stderr=install_stderr, duration_s=install_duration,
                dry_run=False,
            )
        if a[0] == "systemctl":
            return RunResult(
                argv=a, exit_code=enable_exit, stdout="",
                stderr=enable_stderr, duration_s=enable_duration,
                dry_run=False,
            )
        raise RuntimeError(f"unexpected argv in fake runner: {a}")

    return fake_run


def _seed_single_service(tmp_path: Path):
    """Build a 1-entry tconf + a store with one snapshot, return
    (resolved_entries map, plan, store, baselines, src_path).
    """
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
    entries = tuple(report.entries.values())

    store_root = tmp_path / "store"
    store_root.mkdir()
    store = PlainBackupStore(root=store_root, target_user=_self_user())
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    resolved = [resolve_entry(e, "ubuntu", target_user=_self_user()) for e in entries]
    backup_pending(
        entries=resolved, store=store, baselines=baselines, timestamp="20260101-000000"
    )
    # Live file edited after backup; recovery's restore must put
    # 'backed-up-content' back.
    src.write_text("vendor-stock-after-install", encoding="utf-8")

    plan = build_recovery_plan(
        resolved_entries=resolved, store=store, distro_id="ubuntu"
    )
    resolved_map = {r.entry.id: r for r in resolved}
    return resolved_map, plan, store, baselines, src, store_root


def _seed_app_entry(tmp_path: Path):
    """App entry: no service_unit (→ no enable step), no snapshot in
    store (→ no restore step). Only install is applicable."""
    body = f"""
schema_version: 1
id: neovim
name: Neovim
kind: app
description: x
files:
  - id: conf
    path: {tmp_path / 'init.vim'}
    description: x
distros:
  ubuntu:
    package: neovim
"""
    folder = tmp_path / "tconf"
    folder.mkdir()
    (folder / "neovim.yaml").write_text(body, encoding="utf-8")
    report = load_folders([folder])
    entries = tuple(report.entries.values())

    store_root = tmp_path / "store"
    store_root.mkdir()
    store = PlainBackupStore(root=store_root, target_user=_self_user())
    baselines = BaselineStore.load(store_root, target_user=_self_user())
    resolved = [resolve_entry(e, "ubuntu", target_user=_self_user()) for e in entries]
    plan = build_recovery_plan(
        resolved_entries=resolved, store=store, distro_id="ubuntu"
    )
    return {r.entry.id: r for r in resolved}, plan, store, baselines


# ---------- happy path ----------


def test_recovery_happy_path_all_steps_ok(tmp_path, monkeypatch):
    """One service, install succeeds, restore writes back the user's
    config, enable succeeds — entry rolls up to OK, every step
    carries its argv / counts / duration."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )

    assert report.timestamp == "20260620-153045"
    assert report.distro_id == "ubuntu"
    assert report.dry_run is False
    assert "alphabetical" in report.ordering_note

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.entry_id == "smoke"
    assert entry.status == ENTRY_OK
    assert [s.name for s in entry.steps] == ["install", "restore", "enable"]
    assert all(s.status == STATUS_OK for s in entry.steps)

    install = entry.steps[0]
    assert install.argv == ("apt-get", "install", "-y", "smoke")
    assert install.exit_code == 0
    restore = entry.steps[1]
    assert restore.snapshot == "20260101-000000"
    assert restore.files_restored == 1
    assert restore.files_failed == 0
    enable = entry.steps[2]
    assert enable.argv == ("systemctl", "enable", "--now", "smoke")
    assert enable.exit_code == 0

    # Live file was rewritten back to the user's backed-up content.
    assert src.read_text() == "backed-up-content"


def test_recovery_does_not_take_pre_restore_snapshot(tmp_path, monkeypatch):
    """Joan's decision 2: even when the live file exists on disk (the
    stock vendor copy after install), recovery must not write a
    pre-restore handle. The dir under <store>/<entry>/<TS-pre-restore>
    is the canary."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())

    execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )

    pre_dir = store_root / "smoke" / f"20260620-153045{PRE_RESTORE_SUFFIX}"
    assert not pre_dir.exists(), (
        "recovery must not pollute the store with pre-restore handles "
        "for vendor stock configs"
    )


# ---------- cascade ----------


def test_install_fail_cascades_to_skip_restore_and_enable(tmp_path, monkeypatch):
    """The substance: install fails → restore + enable both skipped
    with reason 'install failed', entry status FAILED. Crucially, the
    LIVE file is not overwritten (the user's stock vendor config
    stays) and no subprocess fires for enable."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)
    monkeypatch.setattr(
        "lazyserver.services.control.run",
        _make_runner(install_exit=100, install_stderr="E: Unable to locate package smoke"),
    )

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )

    entry = report.entries[0]
    assert entry.status == ENTRY_FAILED
    install, restore, enable = entry.steps
    assert install.status == STATUS_FAILED
    assert install.exit_code == 100
    assert install.stderr_tail and "Unable to locate package smoke" in install.stderr_tail
    assert restore.status == STATUS_SKIPPED
    assert restore.reason == CASCADE_REASON
    assert enable.status == STATUS_SKIPPED
    assert enable.reason == CASCADE_REASON

    # Live file untouched — confirms restore really did NOT run.
    assert src.read_text() == "vendor-stock-after-install"


def test_restore_fail_does_not_cascade_enable_still_runs(tmp_path, monkeypatch):
    """Restore failing must NOT cascade to enable — a running service
    on stock config beats a stopped service. Entry rolls up to
    `partial` because both an ok and a failed step coexist."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())

    # Make execute_restore raise so the restore step is FAILED, without
    # otherwise altering the install/enable subprocess outcomes.
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("lazyserver.recovery.run.execute_restore", boom)

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )
    entry = report.entries[0]
    assert entry.status == ENTRY_PARTIAL
    install, restore, enable = entry.steps
    assert install.status == STATUS_OK
    assert restore.status == STATUS_FAILED
    assert restore.snapshot == "20260101-000000"
    assert restore.stderr_tail and "disk full" in restore.stderr_tail
    # Enable ran despite the restore failure.
    assert enable.status == STATUS_OK
    assert enable.exit_code == 0


def test_enable_fail_leaves_install_and_restore_intact(tmp_path, monkeypatch):
    """Enable failing → entry partial (install + restore stand)."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)
    monkeypatch.setattr(
        "lazyserver.services.control.run",
        _make_runner(enable_exit=1, enable_stderr="Failed to enable unit"),
    )

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )
    entry = report.entries[0]
    assert entry.status == ENTRY_PARTIAL
    assert entry.steps[0].status == STATUS_OK  # install
    assert entry.steps[1].status == STATUS_OK  # restore
    assert entry.steps[2].status == STATUS_FAILED  # enable
    assert "Failed to enable unit" in (entry.steps[2].stderr_tail or "")
    # Restore still wrote the file back.
    assert src.read_text() == "backed-up-content"


# ---------- app entry (no enable, no restore) ----------


def test_app_entry_install_ok_restore_and_enable_skipped(tmp_path, monkeypatch):
    """Apps roll up to OK when install succeeds — the other two steps
    are non-applicable by design, not failures."""
    resolved_map, plan, store, baselines = _seed_app_entry(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
    )
    entry = report.entries[0]
    assert entry.entry_id == "neovim"
    assert entry.status == ENTRY_OK
    install, restore, enable = entry.steps
    assert install.status == STATUS_OK
    assert restore.status == STATUS_SKIPPED
    assert "no snapshots" in (restore.reason or "")
    assert enable.status == STATUS_SKIPPED
    assert "app" in (enable.reason or "")


# ---------- dry-run ----------


def test_dry_run_fires_no_subprocess_and_writes_no_files(tmp_path, monkeypatch):
    """Dry-run path: every applicable step becomes WOULD-RUN, the live
    file stays the way it was, no pre-restore dir lands, and the
    monkeypatched runner is never called (defensive — catches any
    accidental execute_install / execute_action slip)."""
    resolved_map, plan, store, baselines, src, store_root = _seed_single_service(tmp_path)

    def boom_run(argv, **kwargs):
        raise AssertionError(f"runner must not fire in dry-run: argv={argv}")

    monkeypatch.setattr("lazyserver.services.control.run", boom_run)

    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260620-153045",
        dry_run=True,
    )

    assert report.dry_run is True
    entry = report.entries[0]
    assert entry.status == ENTRY_WOULD_RUN
    install, restore, enable = entry.steps
    assert install.status == STATUS_WOULD_RUN
    assert install.argv == ("apt-get", "install", "-y", "smoke")
    assert restore.status == STATUS_WOULD_RUN
    assert restore.snapshot == "20260101-000000"
    assert restore.files_restored == 1
    assert enable.status == STATUS_WOULD_RUN
    assert enable.argv == ("systemctl", "enable", "--now", "smoke")

    # Live file unchanged, no pre-restore dir.
    assert src.read_text() == "vendor-stock-after-install"
    pre_dir = store_root / "smoke" / f"20260620-153045{PRE_RESTORE_SUFFIX}"
    assert not pre_dir.exists()


# ---------- plumbing ----------


def test_report_threads_distro_and_ordering_note_through(tmp_path, monkeypatch):
    """The plan-level metadata (distro_id, ordering_note) reaches the
    report — the formatter uses these to disclose ordering and stamp
    the run."""
    resolved_map, plan, store, baselines, _src, _root = _seed_single_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())
    report = execute_recovery(
        plan,
        resolved_entries=resolved_map,
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260101-000000",
    )
    assert report.distro_id == plan.distro_id == "ubuntu"
    assert report.ordering_note == plan.ordering_note


def test_unknown_entry_in_plan_is_marked_skipped_not_raised(tmp_path, monkeypatch):
    """Defensive: an entry in the plan but missing from the
    resolved_entries map produces an all-skipped EntryResult — the
    rest of the plan still runs."""
    resolved_map, plan, store, baselines, _src, _root = _seed_single_service(tmp_path)
    monkeypatch.setattr("lazyserver.services.control.run", _make_runner())
    # Drop the resolved entry to force the missing-entry branch.
    report = execute_recovery(
        plan,
        resolved_entries={},
        store=store,
        baselines=baselines,
        target_user=_self_user(),
        timestamp="20260101-000000",
    )
    entry = report.entries[0]
    assert all(s.status == STATUS_SKIPPED for s in entry.steps)
    assert all("resolved_entries" in (s.reason or "") for s in entry.steps)
