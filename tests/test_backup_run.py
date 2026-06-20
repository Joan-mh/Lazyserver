"""Backup orchestration (FR-2.3/2.4) + orig owner/mode capture.

Orchestration is typed against the BackupStore Protocol; tests use
PlainBackupStore as the concrete and a FakeStore for partial-failure
atomicity.
"""

from __future__ import annotations

import json
import os
import pwd
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lazyserver.backup.checksums import sha256_of
from lazyserver.backup.pending import (
    BASELINES_FILENAME,
    Baseline,
    BaselineStore,
    PendingItem,
    PendingStatus,
    scan_all,
    scan_entry,
)
from lazyserver.backup.run import (
    BackupOutcome,
    BackupReport,
    backup_files,
    backup_pending,
    current_timestamp,
)
from lazyserver.backup.store import BackupStore, SnapshotRef
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.user import TargetUser
from lazyserver.tconf.model import Entry
from lazyserver.tconf.resolve import (
    ResolvedEntry,
    ResolvedFile,
    ResolvedFileSet,
)


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _entry(entry_id: str = "ex", kind: str = "service") -> Entry:
    return Entry(
        schema_version=1,
        id=entry_id,
        name=entry_id,
        kind=kind,
        description="",
        distros={},
    )


def _resolved(
    files: tuple[ResolvedFile, ...] = (),
    file_sets: tuple[ResolvedFileSet, ...] = (),
    entry_id: str = "ex",
) -> ResolvedEntry:
    return ResolvedEntry(
        entry=_entry(entry_id),
        distro_id="arch",
        package="ex",
        service_unit="ex",
        install=(),
        actions={},
        files=files,
        file_sets=file_sets,
        aliases=(),
    )


def _fixed(id_: str, path: Path, *, optional: bool = False) -> ResolvedFile:
    return ResolvedFile(
        id=id_, path=str(path), description="", example=None, optional=optional
    )


def _store_at(tmp_path: Path) -> tuple[PlainBackupStore, BaselineStore]:
    store_root = tmp_path / "store"
    store_root.mkdir()
    user = _self_user()
    return (
        PlainBackupStore(root=store_root, target_user=user),
        BaselineStore.load(store_root, target_user=user),
    )


# ---------- baseline backward compat ----------


def test_baseline_round_trips_orig_uid_gid_mode(tmp_path: Path):
    store = BaselineStore.load(tmp_path)
    store.set(
        "bind9",
        Path("/etc/named.conf"),
        Baseline(
            sha256="a" * 64,
            snapshot="t1",
            file_id="named_conf",
            set_id=None,
            orig_uid=999,
            orig_gid=998,
            orig_mode=0o640,
        ),
    )
    store.save()
    reloaded = BaselineStore.load(tmp_path)
    bl = reloaded.get("bind9", Path("/etc/named.conf"))
    assert bl.orig_uid == 999
    assert bl.orig_gid == 998
    assert bl.orig_mode == 0o640


def test_baseline_loads_legacy_record_without_orig_fields(tmp_path: Path):
    """A v1 ledger written before 4c (no orig_* keys) still parses;
    the missing fields default to None and Phase 5 will treat them as
    'unknown — preserve current ownership instead'."""
    payload = {
        "version": 1,
        "entries": {
            "bind9": {
                "files": {
                    "/etc/named.conf": {
                        "sha256": "f" * 64,
                        "snapshot": "t0",
                        "file_id": "named_conf",
                        "set_id": None,
                    }
                }
            }
        },
    }
    (tmp_path / BASELINES_FILENAME).write_text(json.dumps(payload))
    store = BaselineStore.load(tmp_path)
    bl = store.get("bind9", Path("/etc/named.conf"))
    assert bl.orig_uid is None
    assert bl.orig_gid is None
    assert bl.orig_mode is None


def test_baseline_omits_orig_fields_in_payload_when_none(tmp_path: Path):
    """Keep the schema additive: when orig_* aren't captured, don't
    write null keys; legacy reading code expects them to simply be
    absent."""
    store = BaselineStore.load(tmp_path)
    store.set(
        "ex",
        Path("/etc/x"),
        Baseline(sha256="a" * 64, snapshot="t0", file_id="x", set_id=None),
    )
    store.save()
    payload = json.loads((tmp_path / BASELINES_FILENAME).read_text())
    rec = payload["entries"]["ex"]["files"]["/etc/x"]
    assert "orig_uid" not in rec
    assert "orig_gid" not in rec
    assert "orig_mode" not in rec


# ---------- backup_pending: happy path + orig capture ----------


def test_backup_pending_snapshots_new_and_changed_only(tmp_path: Path):
    """UNCHANGED is skipped; NEW + CHANGED are backed up."""
    p_new = tmp_path / "new.conf"
    p_new.write_text("new content\n")
    p_changed = tmp_path / "changed.conf"
    p_changed.write_text("v2\n")
    p_unchanged = tmp_path / "unchanged.conf"
    p_unchanged.write_text("steady\n")

    store, baselines = _store_at(tmp_path)
    # Seed baselines: p_changed has v1 sha, p_unchanged is up-to-date.
    baselines.set("ex", p_changed, Baseline(
        sha256="0" * 64, snapshot="old", file_id="changed", set_id=None,
    ))
    baselines.set("ex", p_unchanged, Baseline(
        sha256=sha256_of(p_unchanged), snapshot="old",
        file_id="unchanged", set_id=None,
    ))

    r = _resolved(files=(
        _fixed("new", p_new),
        _fixed("changed", p_changed),
        _fixed("unchanged", p_unchanged),
    ))
    reports = backup_pending(
        entries=[r], store=store, baselines=baselines, timestamp="t1",
    )

    by_path = {rep.item.path: rep for rep in reports}
    assert by_path[p_new].outcome is BackupOutcome.BACKED_UP
    assert by_path[p_changed].outcome is BackupOutcome.BACKED_UP
    assert by_path[p_unchanged].outcome is BackupOutcome.SKIPPED_UNCHANGED


def test_backup_pending_updates_baseline_with_current_sha(tmp_path: Path):
    p = tmp_path / "named.conf"
    p.write_text("v1\n")
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("named_conf", p),))
    backup_pending(entries=[r], store=store, baselines=baselines, timestamp="t1")

    bl = baselines.get("ex", p)
    assert bl is not None
    assert bl.sha256 == sha256_of(p)
    assert bl.snapshot == "t1"
    assert bl.file_id == "named_conf"
    assert bl.set_id is None


def test_backup_pending_captures_orig_uid_gid_mode(tmp_path: Path):
    """The Baseline records the *source* file's stat, not the store's.
    Phase 5 will use these to restore back as service-owned, not
    target_user-owned."""
    p = tmp_path / "named.conf"
    p.write_text("x")
    os.chmod(p, 0o640)
    expected_st = p.stat()
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("named_conf", p),))
    backup_pending(entries=[r], store=store, baselines=baselines, timestamp="t1")

    bl = baselines.get("ex", p)
    assert bl.orig_uid == expected_st.st_uid
    assert bl.orig_gid == expected_st.st_gid
    assert bl.orig_mode == 0o640


def test_backup_pending_persists_baseline_to_disk(tmp_path: Path):
    p = tmp_path / "named.conf"
    p.write_text("x")
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("named_conf", p),))
    backup_pending(entries=[r], store=store, baselines=baselines, timestamp="t1")
    # Re-load from disk: the ledger persisted.
    reloaded = BaselineStore.load(store.root, target_user=_self_user())
    assert reloaded.get("ex", p) is not None


def test_backup_pending_does_not_save_when_nothing_eligible(tmp_path: Path):
    """All UNCHANGED → ledger file should not be created on save()."""
    p = tmp_path / "f"
    p.write_text("x")
    store, baselines = _store_at(tmp_path)
    baselines.set("ex", p, Baseline(
        sha256=sha256_of(p), snapshot="old", file_id="f", set_id=None,
    ))
    backup_pending(entries=[_resolved(files=(_fixed("f", p),))],
                   store=store, baselines=baselines, timestamp="t1")
    assert not (store.root / BASELINES_FILENAME).exists()


# ---------- non-eligible statuses are reported, not skipped silently ----------


def test_backup_pending_reports_missing_as_skipped_missing(tmp_path: Path):
    """A baselined-but-gone file is reported, not silently dropped."""
    p = tmp_path / "named.conf"  # never written
    store, baselines = _store_at(tmp_path)
    baselines.set("ex", p, Baseline(
        sha256="0" * 64, snapshot="old", file_id="named_conf", set_id=None,
    ))
    r = _resolved(files=(_fixed("named_conf", p),))
    reports = backup_pending(
        entries=[r], store=store, baselines=baselines, timestamp="t1",
    )
    [rep] = reports
    assert rep.outcome is BackupOutcome.SKIPPED_MISSING
    assert rep.ref is None
    # Baseline must not be touched: still points at the old snapshot.
    assert baselines.get("ex", p).snapshot == "old"


def test_backup_pending_reports_absent_required(tmp_path: Path):
    p = tmp_path / "missing.conf"
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("named_conf", p, optional=False),))
    [rep] = backup_pending(
        entries=[r], store=store, baselines=baselines, timestamp="t1",
    )
    assert rep.outcome is BackupOutcome.SKIPPED_ABSENT


# ---------- backup_files: explicit selection ----------


def test_backup_files_only_acts_on_provided_items(tmp_path: Path):
    a = tmp_path / "a.conf"; a.write_text("A")
    b = tmp_path / "b.conf"; b.write_text("B")
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("a", a), _fixed("b", b)))
    [scan_a, scan_b] = scan_entry(r, baselines)
    # Caller picks only `a`.
    only_a = scan_a if scan_a.path == a else scan_b
    reports = backup_files(
        items=[only_a], store=store, baselines=baselines, timestamp="t1",
    )
    [rep] = reports
    assert rep.item.path == a
    assert rep.outcome is BackupOutcome.BACKED_UP
    assert baselines.get("ex", a) is not None
    assert baselines.get("ex", b) is None


# ---------- partial-failure atomicity ----------


@dataclass
class _FakeStore:
    """BackupStore impl that fails on the Nth snapshot call."""
    root: Path
    fail_after: int = 1  # number of successes before raising
    calls: int = field(default=0)
    inner: PlainBackupStore | None = field(default=None)

    def snapshot(self, *, entry_id, source, timestamp, metadata):
        if self.calls >= self.fail_after:
            self.calls += 1
            raise OSError("synthetic store failure")
        self.calls += 1
        return self.inner.snapshot(
            entry_id=entry_id, source=source, timestamp=timestamp, metadata=metadata,
        )

    def list_snapshots(self, entry_id): return self.inner.list_snapshots(entry_id)
    def list_files(self, entry_id, timestamp): return self.inner.list_files(entry_id, timestamp)
    def read(self, ref): return self.inner.read(ref)
    def read_metadata(self, entry_id, timestamp): return self.inner.read_metadata(entry_id, timestamp)
    def commit_operation(self, *, message): return self.inner.commit_operation(message=message)


def test_partial_failure_leaves_baselines_coherent(tmp_path: Path):
    """First snapshot succeeds, second fails: the first's baseline is
    persisted, the second's is left unset. The ledger is never in a
    state where it claims something is backed up that isn't on disk."""
    a = tmp_path / "a"; a.write_text("A")
    b = tmp_path / "b"; b.write_text("B")
    store_root = tmp_path / "store"
    store_root.mkdir()
    user = _self_user()
    inner = PlainBackupStore(root=store_root, target_user=user)
    failing = _FakeStore(root=store_root, fail_after=1, inner=inner)
    baselines = BaselineStore.load(store_root, target_user=user)

    r = _resolved(files=(_fixed("a", a), _fixed("b", b)))
    reports = backup_pending(
        entries=[r], store=failing, baselines=baselines, timestamp="t1",
    )

    by_path = {rep.item.path: rep for rep in reports}
    assert by_path[a].outcome is BackupOutcome.BACKED_UP
    assert by_path[b].outcome is BackupOutcome.FAILED
    assert by_path[b].error and "synthetic" in by_path[b].error

    # Persisted ledger: only `a` is there.
    reloaded = BaselineStore.load(store_root, target_user=user)
    assert reloaded.get("ex", a) is not None
    assert reloaded.get("ex", b) is None


# ---------- protocol typing ----------


def test_orchestration_typed_against_protocol():
    """4c is typed against BackupStore — not PlainBackupStore — so 4d's
    GitBackupStore drops in via the same call sites."""
    import typing

    from lazyserver.backup import run as run_mod

    hints = typing.get_type_hints(run_mod.backup_pending)
    assert hints["store"] is BackupStore
    hints_files = typing.get_type_hints(run_mod.backup_files)
    assert hints_files["store"] is BackupStore


# ---------- timestamps ----------


def test_timestamp_format_is_iso_compact():
    ts = current_timestamp()
    # Shape: 8 digits + '-' + 6 digits.
    assert len(ts) == 15 and ts[8] == "-"
    int(ts[:8])  # parses
    int(ts[9:])  # parses


def test_timestamp_shared_across_one_operation(tmp_path: Path):
    """All snapshots in one call land under the same <ts> directory,
    so a snapshot is a unit in the eyes of `list_snapshots`."""
    a = tmp_path / "a"; a.write_text("A")
    b = tmp_path / "b"; b.write_text("B")
    store, baselines = _store_at(tmp_path)
    r = _resolved(files=(_fixed("a", a), _fixed("b", b)))
    backup_pending(entries=[r], store=store, baselines=baselines, timestamp="ts-only-one")
    assert store.list_snapshots("ex") == ["ts-only-one"]
    files = store.list_files("ex", "ts-only-one")
    # Both source paths captured under the same timestamp.
    assert len(files) == 2
