"""FR-2.1/2.2 — baseline ledger persistence + scan classification.

The ledger is small enough to test directly through its public API; the
scan tests build a ResolvedEntry by hand so they don't depend on the
bundled tconf files. Together they pin down all six PendingStatus
classifications, including the MISSING and ABSENT_REQUIRED cases that
the user note asks be surfaced rather than hidden.
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup.checksums import sha256_of
from lazyserver.backup.pending import (
    BASELINES_FILENAME,
    Baseline,
    BaselineStore,
    PendingStatus,
    scan_all,
    scan_entry,
    pending_only,
)
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


# ---------- ledger persistence ----------


def _bl(sha: str = "a" * 64, file_id: str = "x", set_id: str | None = None) -> Baseline:
    return Baseline(sha256=sha, snapshot="20260618-103045", file_id=file_id, set_id=set_id)


def test_ledger_load_returns_empty_when_file_missing(tmp_path: Path):
    store = BaselineStore.load(tmp_path)
    assert store.get("anything", Path("/etc/x")) is None


def test_ledger_in_memory_when_root_is_none():
    store = BaselineStore.load(None)
    store.set("bind9", Path("/etc/named.conf"), _bl())
    store.save()  # no-op, must not raise
    assert store.get("bind9", Path("/etc/named.conf")) is not None


def test_ledger_round_trips(tmp_path: Path):
    store = BaselineStore.load(tmp_path)
    store.set("bind9", Path("/etc/named.conf"), _bl(sha="f" * 64, file_id="named_conf"))
    store.set("bind9", Path("/etc/bind/db.a"), _bl(sha="b" * 64, file_id="zone_files", set_id="zone_files"))
    store.set("nginx", Path("/etc/nginx/nginx.conf"), _bl(file_id="nginx_conf"))
    store.save()

    reopened = BaselineStore.load(tmp_path)
    assert reopened.get("bind9", Path("/etc/named.conf")).sha256 == "f" * 64
    assert reopened.get("bind9", Path("/etc/bind/db.a")).set_id == "zone_files"
    assert reopened.get("nginx", Path("/etc/nginx/nginx.conf")).file_id == "nginx_conf"


def test_ledger_write_is_atomic(tmp_path: Path):
    """No `.tmp` file is left behind after a successful save."""
    store = BaselineStore.load(tmp_path)
    store.set("bind9", Path("/etc/named.conf"), _bl())
    store.save()
    target = tmp_path / BASELINES_FILENAME
    assert target.exists()
    assert not (tmp_path / (BASELINES_FILENAME + ".tmp")).exists()


def test_ledger_rejects_unknown_schema_version(tmp_path: Path):
    bogus = {"version": 99, "entries": {}}
    (tmp_path / BASELINES_FILENAME).write_text(json.dumps(bogus))
    with pytest.raises(ValueError, match="schema version"):
        BaselineStore.load(tmp_path)


def test_ledger_save_chowns_to_target_user(tmp_path: Path):
    """4b ownership boundary: the saved ledger lands target-user-owned."""
    user = _self_user()
    store = BaselineStore.load(tmp_path, target_user=user)
    store.set("bind9", Path("/etc/named.conf"), _bl())
    store.save()
    saved = tmp_path / BASELINES_FILENAME
    assert saved.stat().st_uid == user.uid


def test_ledger_save_does_not_chown_pre_existing_root(tmp_path: Path):
    """The configured backup_store root predates us — we never modify
    its ownership/mode."""
    root = tmp_path / "store"
    root.mkdir(mode=0o701)
    root_before = root.stat()

    store = BaselineStore.load(root, target_user=_self_user())
    store.set("bind9", Path("/etc/named.conf"), _bl())
    store.save()

    after = root.stat()
    assert after.st_uid == root_before.st_uid
    assert after.st_mode == root_before.st_mode


def test_ledger_iter_entry_yields_baselined_paths(tmp_path: Path):
    store = BaselineStore.load(tmp_path)
    store.set("bind9", Path("/etc/bind/db.a"), _bl(sha="1" * 64, file_id="zone_files", set_id="zone_files"))
    store.set("bind9", Path("/etc/bind/db.b"), _bl(sha="2" * 64, file_id="zone_files", set_id="zone_files"))
    store.set("nginx", Path("/etc/nginx/nginx.conf"), _bl(file_id="nginx_conf"))
    paths = {p for p, _ in store.iter_entry("bind9")}
    assert paths == {Path("/etc/bind/db.a"), Path("/etc/bind/db.b")}


# ---------- scan classification ----------


def _entry(entry_id: str = "ex", kind: str = "service") -> Entry:
    # Minimal Entry — the scan only reaches for .id and .kind.
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


def _fixed(id: str, path: Path, *, optional: bool = False) -> ResolvedFile:
    return ResolvedFile(
        id=id, path=str(path), description="", example=None, optional=optional
    )


def _set(id: str, directory: Path, pattern: str = "db.*") -> ResolvedFileSet:
    return ResolvedFileSet(
        id=id,
        directory=str(directory),
        pattern=pattern,
        description="",
        example=None,
        optional=True,
        owner=None,
        group=None,
        mode=None,
    )


def test_fixed_file_with_no_baseline_is_new(tmp_path: Path):
    p = tmp_path / "named.conf"
    p.write_text("x")
    r = _resolved(files=(_fixed("named_conf", p),))
    store = BaselineStore.load(None)
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.NEW
    assert item.current_sha is not None
    assert item.baseline_sha is None


def test_fixed_file_unchanged_when_baseline_matches(tmp_path: Path):
    p = tmp_path / "named.conf"
    p.write_text("x")
    sha = sha256_of(p)
    r = _resolved(files=(_fixed("named_conf", p),))
    store = BaselineStore.load(None)
    store.set("ex", p, Baseline(sha256=sha, snapshot="t0", file_id="named_conf", set_id=None))
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.UNCHANGED


def test_fixed_file_changed_when_sha_differs(tmp_path: Path):
    p = tmp_path / "named.conf"
    p.write_text("v1")
    r = _resolved(files=(_fixed("named_conf", p),))
    store = BaselineStore.load(None)
    store.set("ex", p, Baseline(sha256="0" * 64, snapshot="t0", file_id="named_conf", set_id=None))
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.CHANGED
    assert item.current_sha is not None
    assert item.baseline_sha == "0" * 64


def test_fixed_file_missing_when_baseline_exists_and_disk_does_not(tmp_path: Path):
    """User's reminder: surface, don't hide. Deleted file → MISSING, not skipped."""
    p = tmp_path / "named.conf"
    r = _resolved(files=(_fixed("named_conf", p),))
    store = BaselineStore.load(None)
    store.set("ex", p, Baseline(sha256="f" * 64, snapshot="t0", file_id="named_conf", set_id=None))
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.MISSING
    assert item.current_sha is None
    assert item.baseline_sha == "f" * 64
    # And it makes it into the pending list.
    assert pending_only([item]) == [item]


def test_fixed_file_absent_optional_is_silent(tmp_path: Path):
    p = tmp_path / "ssl.conf"
    r = _resolved(files=(_fixed("ssl_conf", p, optional=True),))
    [item] = scan_entry(r, BaselineStore.load(None))
    assert item.status is PendingStatus.ABSENT_OPTIONAL
    assert pending_only([item]) == []


def test_fixed_file_absent_required_is_surfaced(tmp_path: Path):
    """A required file never seen on disk: not backup-eligible, but the
    user should know it's missing — not silently skipped."""
    p = tmp_path / "named.conf"
    r = _resolved(files=(_fixed("named_conf", p, optional=False),))
    [item] = scan_entry(r, BaselineStore.load(None))
    assert item.status is PendingStatus.ABSENT_REQUIRED
    assert pending_only([item]) == [item]
    assert item.status.is_backup_eligible() is False


def test_set_member_new_at_scan_time(tmp_path: Path):
    """FR-1.6: glob runs at scan time, so files created after baselines
    were saved are picked up."""
    zones = tmp_path / "zones"
    zones.mkdir()
    r = _resolved(file_sets=(_set("zone_files", zones),))
    # Saved baselines: empty. Then create a file.
    (zones / "db.example").write_text("x")
    items = scan_entry(r, BaselineStore.load(None))
    paths = {i.path for i in items}
    assert zones / "db.example" in paths
    [item] = [i for i in items if i.path == zones / "db.example"]
    assert item.status is PendingStatus.NEW
    assert item.set_id == "zone_files"


def test_set_member_unchanged_then_changed(tmp_path: Path):
    zones = tmp_path / "zones"
    zones.mkdir()
    f = zones / "db.a"
    f.write_text("v1")
    sha_v1 = sha256_of(f)
    r = _resolved(file_sets=(_set("zone_files", zones),))
    store = BaselineStore.load(None)
    store.set("ex", f, Baseline(sha256=sha_v1, snapshot="t0", file_id="zone_files", set_id="zone_files"))
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.UNCHANGED

    f.write_text("v2")
    [item] = scan_entry(r, store)
    assert item.status is PendingStatus.CHANGED


def test_set_member_deleted_surfaces_as_missing(tmp_path: Path):
    """Baseline lists the file but the glob no longer matches it."""
    zones = tmp_path / "zones"
    zones.mkdir()
    gone = zones / "db.gone"
    r = _resolved(file_sets=(_set("zone_files", zones),))
    store = BaselineStore.load(None)
    store.set("ex", gone, Baseline(sha256="9" * 64, snapshot="t0", file_id="zone_files", set_id="zone_files"))
    [item] = scan_entry(r, store)
    assert item.path == gone
    assert item.status is PendingStatus.MISSING
    assert item.set_id == "zone_files"


def test_scan_orders_by_path(tmp_path: Path):
    a = tmp_path / "a"; a.write_text("1")
    b = tmp_path / "b"; b.write_text("2")
    r = _resolved(files=(_fixed("a", a), _fixed("b", b)))
    items = scan_entry(r, BaselineStore.load(None))
    assert [i.path for i in items] == [a, b]


def test_scan_all_groups_by_entry_then_path(tmp_path: Path):
    e1 = _resolved(
        entry_id="alpha",
        files=(_fixed("f", tmp_path / "alpha.conf"),),
    )
    e2 = _resolved(
        entry_id="zulu",
        files=(_fixed("f", tmp_path / "zulu.conf"),),
    )
    (tmp_path / "alpha.conf").write_text("a")
    (tmp_path / "zulu.conf").write_text("z")
    items = scan_all([e2, e1], BaselineStore.load(None))
    assert [i.entry_id for i in items] == ["alpha", "zulu"]


def test_pending_status_helpers():
    assert PendingStatus.NEW.is_backup_eligible()
    assert PendingStatus.CHANGED.is_backup_eligible()
    assert not PendingStatus.UNCHANGED.is_backup_eligible()
    assert not PendingStatus.MISSING.is_backup_eligible()
    assert not PendingStatus.ABSENT_OPTIONAL.is_backup_eligible()
    assert not PendingStatus.ABSENT_REQUIRED.is_backup_eligible()

    assert PendingStatus.NEW.is_pending()
    assert PendingStatus.MISSING.is_pending()
    assert PendingStatus.ABSENT_REQUIRED.is_pending()
    assert not PendingStatus.UNCHANGED.is_pending()
    assert not PendingStatus.ABSENT_OPTIONAL.is_pending()
