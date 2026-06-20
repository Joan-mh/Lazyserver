"""PlainBackupStore — content round-trip + ownership boundary (FR-2.5)."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup.store import FileMetadata
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.user import TargetUser


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _store(root: Path, *, target_user: TargetUser | None = None) -> PlainBackupStore:
    root.mkdir(parents=True, exist_ok=True)
    return PlainBackupStore(root=root, target_user=target_user or _self_user())


def _meta(sha: str = "0" * 64, *, uid: int = 0, gid: int = 0, mode: int = 0o644) -> FileMetadata:
    """Stub metadata for store tests — exact values don't matter unless the
    test asserts on them; we just need a valid FileMetadata to pass through."""
    return FileMetadata(uid=uid, gid=gid, mode=mode, sha256=sha)


# ---------- snapshot / round-trip ----------


def test_snapshot_writes_content_byte_identically(tmp_path: Path):
    src = tmp_path / "named.conf"
    src.write_bytes(b"options { recursion no; };\n")
    store = _store(tmp_path / "store")

    ref = store.snapshot(entry_id="bind9", source=src, timestamp="20260618-103045", metadata=_meta())
    assert ref.entry_id == "bind9"
    assert ref.timestamp == "20260618-103045"
    assert ref.source_path == src
    assert store.read(ref) == b"options { recursion no; };\n"


def test_stored_path_mirrors_source_under_entry_timestamp(tmp_path: Path):
    src = tmp_path / "etc" / "named.conf"
    src.parent.mkdir()
    src.write_bytes(b"x")
    root = tmp_path / "store"
    store = _store(root)

    ref = store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    expected = root / "bind9" / "t1" / Path(*src.parts[1:])
    assert ref.stored_path == expected
    assert ref.stored_path.exists()


def test_snapshot_refuses_silent_overwrite(tmp_path: Path):
    src = tmp_path / "a"
    src.write_bytes(b"v1")
    store = _store(tmp_path / "store")
    store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=_meta())
    with pytest.raises(FileExistsError, match="snapshot already exists"):
        store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=_meta())


def test_snapshot_requires_absolute_source(tmp_path: Path):
    store = _store(tmp_path / "store")
    with pytest.raises(ValueError, match="must be absolute"):
        store.snapshot(entry_id="e", source=Path("relative.conf"), timestamp="t1", metadata=_meta())


def test_list_snapshots_returns_chronological_order(tmp_path: Path):
    src = tmp_path / "f"
    src.write_bytes(b"x")
    src2 = tmp_path / "f2"
    src2.write_bytes(b"y")
    store = _store(tmp_path / "store")
    store.snapshot(entry_id="e", source=src, timestamp="20260101-000000", metadata=_meta())
    store.snapshot(entry_id="e", source=src, timestamp="20260102-000000", metadata=_meta())
    store.snapshot(entry_id="e", source=src2, timestamp="20260103-000000", metadata=_meta())
    assert store.list_snapshots("e") == [
        "20260101-000000",
        "20260102-000000",
        "20260103-000000",
    ]


def test_list_snapshots_empty_when_no_entry(tmp_path: Path):
    store = _store(tmp_path / "store")
    assert store.list_snapshots("never-touched") == []


def test_list_files_reconstructs_source_paths(tmp_path: Path):
    src1 = tmp_path / "etc" / "named.conf"
    src1.parent.mkdir()
    src1.write_bytes(b"1")
    src2 = tmp_path / "etc" / "bind" / "db.example"
    src2.parent.mkdir(parents=True)
    src2.write_bytes(b"2")
    store = _store(tmp_path / "store")

    store.snapshot(entry_id="bind9", source=src1, timestamp="t1", metadata=_meta())
    store.snapshot(entry_id="bind9", source=src2, timestamp="t1", metadata=_meta())
    listed = store.list_files("bind9", "t1")
    # Sources land under '/' in the listing — symmetric with _stored_path.
    expected = sorted([Path("/") / Path(*src.parts[1:]) for src in (src1, src2)])
    assert listed == expected


# ---------- ownership boundary ----------


def test_pre_existing_store_root_is_not_chowned(tmp_path: Path):
    """The configured root predates us; PlainBackupStore must not
    modify its ownership or mode."""
    root = tmp_path / "store"
    root.mkdir(mode=0o701)  # deliberately odd
    root_stat_before = root.stat()

    src = tmp_path / "x"
    src.write_bytes(b"x")
    store = PlainBackupStore(root=root, target_user=_self_user())
    store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=_meta())

    assert root.stat().st_mode == root_stat_before.st_mode
    assert root.stat().st_uid == root_stat_before.st_uid
    assert root.stat().st_ino == root_stat_before.st_ino


def test_created_directories_carry_target_user_uid(tmp_path: Path):
    """Every directory we create under the store root is owned by
    target_user. We walk from the stored file up to (but not including)
    the root and assert each ancestor's uid."""
    root = tmp_path / "store"
    root.mkdir()
    src = tmp_path / "etc" / "named.conf"
    src.parent.mkdir()
    src.write_bytes(b"x")

    user = _self_user()
    store = PlainBackupStore(root=root, target_user=user)
    ref = store.snapshot(entry_id="bind9", source=src, timestamp="20260618-103045", metadata=_meta())

    resolved_root = root.resolve()
    walker = ref.stored_path.parent
    seen_any = False
    while walker != resolved_root:
        assert walker.is_dir(), walker
        assert walker.stat().st_uid == user.uid, walker
        seen_any = True
        walker = walker.parent
    assert seen_any, "test should have walked at least one created ancestor"


def test_stored_content_file_carries_target_user_uid_and_default_mode(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    src = tmp_path / "named.conf"
    src.write_bytes(b"x")
    user = _self_user()
    store = PlainBackupStore(root=root, target_user=user)
    ref = store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    st = ref.stored_path.stat()
    assert st.st_uid == user.uid
    assert oct(st.st_mode & 0o7777) == "0o644"


def test_target_user_none_still_writes_content(tmp_path: Path):
    """Useful for tests / sessions with no configured target user."""
    root = tmp_path / "store"
    root.mkdir()
    src = tmp_path / "x"
    src.write_bytes(b"x")
    store = PlainBackupStore(root=root, target_user=None)
    ref = store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=_meta())
    assert store.read(ref) == b"x"


# ---------- metadata.json (Phase 5 prep, FR-3.3) ----------


def test_commit_operation_writes_metadata_json_for_each_snapshot(tmp_path: Path):
    """A backup operation produces one metadata.json per (entry, timestamp)
    capturing every snapshotted source's owner/group/mode/sha — the
    record Phase 5 restore reads to put files back as e.g. bind:bind 0640."""
    import json
    src1 = tmp_path / "etc" / "named.conf"
    src1.parent.mkdir()
    src1.write_bytes(b"options { };\n")
    src2 = tmp_path / "etc" / "named.conf.local"
    src2.write_bytes(b"zone { };\n")
    store = _store(tmp_path / "store")

    m1 = FileMetadata(uid=121, gid=127, mode=0o640, sha256="a" * 64)
    m2 = FileMetadata(uid=121, gid=127, mode=0o600, sha256="b" * 64)
    store.snapshot(entry_id="bind9", source=src1, timestamp="t1", metadata=m1)
    store.snapshot(entry_id="bind9", source=src2, timestamp="t1", metadata=m2)
    store.commit_operation(message="backup t1")

    meta_path = tmp_path / "store" / "bind9" / "t1" / "metadata.json"
    assert meta_path.exists()
    raw = json.loads(meta_path.read_text())
    assert raw["schema_version"] == 1
    files = raw["files"]
    assert files[str(src1)] == {"uid": 121, "gid": 127, "mode": 0o640, "sha256": "a" * 64}
    assert files[str(src2)] == {"uid": 121, "gid": 127, "mode": 0o600, "sha256": "b" * 64}


def test_list_files_skips_metadata_json(tmp_path: Path):
    """metadata.json is store bookkeeping, not a captured source path —
    list_files (used by restore to enumerate snapshot contents) must
    not surface it."""
    src = tmp_path / "etc" / "named.conf"
    src.parent.mkdir()
    src.write_bytes(b"x")
    store = _store(tmp_path / "store")
    store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    store.commit_operation(message="m")
    listed = store.list_files("bind9", "t1")
    assert all("metadata.json" not in str(p) for p in listed)
    assert listed == [Path("/") / Path(*src.parts[1:])]


def test_read_metadata_returns_parsed_records(tmp_path: Path):
    src = tmp_path / "etc" / "x"
    src.parent.mkdir()
    src.write_bytes(b"x")
    store = _store(tmp_path / "store")
    meta_in = FileMetadata(uid=33, gid=33, mode=0o644, sha256="c" * 64)
    store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=meta_in)
    store.commit_operation(message="m")

    out = store.read_metadata("e", "t1")
    assert out is not None
    assert out[src] == meta_in


def test_read_metadata_returns_none_for_pre_phase_5_snapshots(tmp_path: Path):
    """Backward-compat signal: VM-verified Phase 4 snapshots have no
    metadata.json. read_metadata must return None (rather than raising)
    so restore can fall back to FR-1.8 ownership resolution."""
    snap_dir = tmp_path / "store" / "old" / "20260101-000000"
    snap_dir.mkdir(parents=True)
    # Plausible legacy layout: a snapshotted file but no metadata.json.
    (snap_dir / "etc").mkdir()
    (snap_dir / "etc" / "old.conf").write_bytes(b"legacy")

    store = _store(tmp_path / "store")
    assert store.read_metadata("old", "20260101-000000") is None
