"""GitBackupStore + make_backup_store factory (FR-2.5).

The Plain layout assertions (round-trip, ownership, refusal to
overwrite) are exercised by test_backup_store_plain.py and trusted
here via the composition — these tests focus on the git-specific
behaviours: detection, init, commit-per-operation, and the
swallow-not-raise contract for history failures.
"""

from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path

import pytest

from lazyserver.backup.store import FileMetadata, make_backup_store
from lazyserver.backup.store_plain import PlainBackupStore
from lazyserver.platform.user import TargetUser


def _meta(sha: str = "0" * 64, *, uid: int = 0, gid: int = 0, mode: int = 0o644) -> FileMetadata:
    """Stub metadata for store tests; values don't matter unless asserted."""
    return FileMetadata(uid=uid, gid=gid, mode=mode, sha256=sha)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary required for GitBackupStore tests",
)

# Imported after the skipif guard so the module is importable on
# git-less systems (it doesn't itself require git at import time, but
# keeping the symmetry with where the tests live).
from lazyserver.backup.store_git import GitBackupStore  # noqa: E402


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


def _git(root: Path, *args: str) -> str:
    """Test helper: read-only git invocation against the store."""
    import subprocess

    completed = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


# ---------- factory ----------


def test_factory_picks_git_when_available(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    store = make_backup_store(root, target_user=_self_user())
    assert isinstance(store, GitBackupStore)


def test_factory_falls_back_to_plain_when_git_absent(tmp_path: Path, monkeypatch):
    """No git on PATH → PlainBackupStore. We patch shutil.which used by
    the factory rather than the binary itself."""
    import lazyserver.backup.store as store_mod

    monkeypatch.setattr(store_mod.shutil, "which", lambda _name: None)
    root = tmp_path / "store"
    root.mkdir()
    store = make_backup_store(root, target_user=_self_user())
    assert isinstance(store, PlainBackupStore)


# ---------- init ----------


def test_construction_initialises_repo_on_empty_root(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    GitBackupStore(root=root, target_user=_self_user())
    assert (root / ".git").is_dir()
    # And it's configured enough to commit (user.email is set locally).
    out = _git(root, "config", "--local", "user.email")
    assert out.strip().endswith("@lazyserver.local")


def test_construction_is_idempotent_on_existing_repo(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    GitBackupStore(root=root, target_user=_self_user())
    head_before = (root / ".git" / "HEAD").read_bytes()
    GitBackupStore(root=root, target_user=_self_user())
    head_after = (root / ".git" / "HEAD").read_bytes()
    assert head_before == head_after


def test_init_chowns_git_dir_to_target_user(tmp_path: Path):
    """`.git/**` must end up target_user-owned so the student can use
    their own git commands on the archive without sudo."""
    root = tmp_path / "store"
    root.mkdir()
    user = _self_user()
    GitBackupStore(root=root, target_user=user)
    # Sample a handful of paths under .git/.
    for sub in (".git", ".git/HEAD", ".git/config", ".git/objects", ".git/refs"):
        p = root / sub
        assert p.exists(), p
        assert p.stat().st_uid == user.uid, p


# ---------- snapshot + commit_operation ----------


def test_snapshot_delegates_layout_to_plain(tmp_path: Path):
    """One snapshot through the git store lands at the exact path
    a PlainBackupStore would have produced — proves layout parity."""
    src = tmp_path / "named.conf"
    src.write_bytes(b"options { recursion no; };\n")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())
    ref = store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    expected = root / "bind9" / "t1" / Path(*src.parts[1:])
    assert ref.stored_path == expected
    assert ref.stored_path.read_bytes() == b"options { recursion no; };\n"


def test_commit_operation_records_one_commit_per_operation(tmp_path: Path):
    src = tmp_path / "named.conf"
    src.write_bytes(b"v1")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())

    # Before any commit, log is empty.
    assert _git(root, "log", "--oneline", "--all").strip() == ""

    store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    store.commit_operation(message="backup t1")

    log_lines = _git(root, "log", "--oneline").strip().splitlines()
    assert len(log_lines) == 1
    assert "backup t1" in log_lines[0]


def test_commit_operation_is_noop_when_nothing_changed(tmp_path: Path):
    src = tmp_path / "named.conf"
    src.write_bytes(b"v1")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())
    store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    store.commit_operation(message="backup t1")

    # Second commit with no new snapshots: log unchanged.
    store.commit_operation(message="backup t2 (should be skipped)")
    log_lines = _git(root, "log", "--oneline").strip().splitlines()
    assert len(log_lines) == 1


def test_commit_operation_includes_files_from_multiple_snapshots(tmp_path: Path):
    """One operation, two snapshots → one commit covering both."""
    a = tmp_path / "a.conf"; a.write_bytes(b"A")
    b = tmp_path / "b.conf"; b.write_bytes(b"B")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())

    store.snapshot(entry_id="e", source=a, timestamp="t1", metadata=_meta())
    store.snapshot(entry_id="e", source=b, timestamp="t1", metadata=_meta())
    store.commit_operation(message="backup t1")

    files_in_commit = set(
        _git(root, "show", "--name-only", "--pretty=", "HEAD").strip().splitlines()
    )
    # Stored paths are relative to root, mirror of source under entry_id/ts.
    assert f"e/t1/{Path(*a.parts[1:])}" in files_in_commit
    assert f"e/t1/{Path(*b.parts[1:])}" in files_in_commit


def test_commit_operation_does_not_raise_on_git_failure(tmp_path: Path, monkeypatch):
    """Protocol contract: commit_operation logs and swallows history
    failures so the snapshot content (already on disk) isn't shadowed
    by an enhancement-layer error in the caller's report."""
    src = tmp_path / "x"
    src.write_bytes(b"x")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())
    store.snapshot(entry_id="e", source=src, timestamp="t1", metadata=_meta())

    # Force every git invocation to fail.
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic git failure")

    monkeypatch.setattr(store, "_git", _boom)
    # Must not raise.
    store.commit_operation(message="backup t1")
    # Snapshot file is still on disk in plain layout.
    assert (root / "e" / "t1" / Path(*src.parts[1:])).exists()


# ---------- read-side delegation ----------


def test_read_and_list_match_plain_layout(tmp_path: Path):
    src = tmp_path / "named.conf"
    src.write_bytes(b"v1")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())
    ref = store.snapshot(entry_id="bind9", source=src, timestamp="t1", metadata=_meta())
    assert store.read(ref) == b"v1"
    assert store.list_snapshots("bind9") == ["t1"]
    assert store.list_files("bind9", "t1") == [Path("/") / Path(*src.parts[1:])]


def test_metadata_json_is_committed_in_the_git_tree(tmp_path: Path):
    """metadata.json must be staged + committed in the same operation as
    the content it describes — otherwise restore that reads from history
    sees content without owner/mode in the same commit."""
    src = tmp_path / "named.conf"
    src.write_bytes(b"v1")
    root = tmp_path / "store"
    root.mkdir()
    store = GitBackupStore(root=root, target_user=_self_user())
    store.snapshot(
        entry_id="bind9", source=src, timestamp="t1",
        metadata=FileMetadata(uid=121, gid=127, mode=0o640, sha256="a" * 64),
    )
    store.commit_operation(message="backup t1")

    # `git ls-tree HEAD` from the store root should list the metadata file.
    tree = _git(root, "ls-tree", "-r", "--name-only", "HEAD")
    assert "bind9/t1/metadata.json" in tree
