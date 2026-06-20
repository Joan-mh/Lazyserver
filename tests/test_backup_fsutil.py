"""Ownership-aware fs helpers (lazyserver.backup._fsutil).

Production runs as root and chowns to a different user; tests run as
the test user and chown to self (no-op as far as ownership is
concerned, but exercises the call path and atomicity). The boundary
that matters here is *what* gets chowned, not *to whom*.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from lazyserver.backup import _fsutil
from lazyserver.backup._fsutil import (
    ensure_owned_dir,
    mkdir_owned_chain,
    write_owned,
)
from lazyserver.platform.user import TargetUser


def _self_user() -> TargetUser:
    me = pwd.getpwuid(os.getuid())
    return TargetUser(name=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir))


# ---------- mkdir_owned_chain ----------


def test_mkdir_owned_chain_creates_missing_parents(tmp_path: Path):
    leaf = tmp_path / "a" / "b" / "c"
    mkdir_owned_chain(leaf, _self_user(), stop_at=tmp_path)
    assert leaf.is_dir()
    assert (tmp_path / "a" / "b").is_dir()


def test_mkdir_owned_chain_chowns_only_what_it_creates(tmp_path: Path):
    """Pre-existing directories are left alone.

    Simulated by recording mtime+stat before, asserting unchanged after.
    Crucially, the configured root (stop_at) is itself pre-existing.
    """
    pre = tmp_path / "existed-before"
    pre.mkdir()
    pre_stat = pre.stat()

    mkdir_owned_chain(pre / "new", _self_user(), stop_at=tmp_path)

    # Pre-existing dir is intact (uid/mode/inode).
    after = pre.stat()
    assert after.st_uid == pre_stat.st_uid
    assert after.st_mode == pre_stat.st_mode
    assert after.st_ino == pre_stat.st_ino


def test_mkdir_owned_chain_never_touches_stop_at(tmp_path: Path):
    """The configured store root predates us — it is the user's
    ownership choice and we don't override it."""
    root = tmp_path / "store"
    root.mkdir(mode=0o700)  # deliberately weird mode
    # Run as a hypothetical "different" target user: cannot actually
    # change uid without root, but we can verify the root's mode is
    # left unchanged.
    mkdir_owned_chain(root / "bind9" / "20260618-103045", _self_user(), stop_at=root)
    assert (root / "bind9" / "20260618-103045").is_dir()
    assert oct(root.stat().st_mode & 0o7777) == "0o700"


def test_mkdir_owned_chain_refuses_to_create_outside_root(tmp_path: Path):
    root = tmp_path / "store"
    root.mkdir()
    with pytest.raises(ValueError, match="outside backup store root"):
        mkdir_owned_chain(tmp_path / "elsewhere" / "x", _self_user(), stop_at=root)


def test_mkdir_owned_chain_idempotent(tmp_path: Path):
    leaf = tmp_path / "a" / "b"
    mkdir_owned_chain(leaf, _self_user(), stop_at=tmp_path)
    # Second call is a no-op, does not raise.
    mkdir_owned_chain(leaf, _self_user(), stop_at=tmp_path)
    assert leaf.is_dir()


def test_mkdir_owned_chain_target_user_none_still_creates_dirs(tmp_path: Path):
    leaf = tmp_path / "a" / "b"
    mkdir_owned_chain(leaf, None, stop_at=tmp_path)
    assert leaf.is_dir()


# ---------- ensure_owned_dir ----------


def test_ensure_owned_dir_creates_and_returns_true(tmp_path: Path):
    root = tmp_path / "store"
    assert not root.exists()
    assert ensure_owned_dir(root, _self_user()) is True
    assert root.is_dir()


def test_ensure_owned_dir_returns_false_on_existing(tmp_path: Path):
    """A pre-existing root is the student's choice — leave it alone.
    Mirrors the `stop_at` contract in mkdir_owned_chain."""
    root = tmp_path / "store"
    root.mkdir(mode=0o700)  # deliberately weird mode
    pre_stat = root.stat()
    assert ensure_owned_dir(root, _self_user()) is False
    after = root.stat()
    # No mode flip, no ownership change, no recreation.
    assert after.st_mode == pre_stat.st_mode
    assert after.st_ino == pre_stat.st_ino


def test_ensure_owned_dir_chowns_root_when_creating(tmp_path, monkeypatch):
    """The core guarantee: a store root LazyServer creates must end up
    owned by the target user, not by root. We can't actually change uid
    without root, so we spy on `os.chown` to prove the call goes through
    with the target user's uid/gid."""
    calls: list[tuple[str, int, int]] = []

    def fake_chown(path, uid, gid):
        calls.append((str(path), uid, gid))

    monkeypatch.setattr(_fsutil.os, "chown", fake_chown)

    target = TargetUser(name="alice", uid=4242, gid=4243, home=tmp_path)
    root = tmp_path / "fresh-store"
    assert ensure_owned_dir(root, target) is True

    chowned = [c for c in calls if c[0] == str(root)]
    assert chowned == [(str(root), 4242, 4243)]


def test_ensure_owned_dir_does_not_chown_when_path_existed(tmp_path, monkeypatch):
    """Pre-existing path → no chown attempted at all (4b: student's choice)."""
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(_fsutil.os, "chown", lambda p, u, g: calls.append((str(p), u, g)))

    root = tmp_path / "store"
    root.mkdir()
    target = TargetUser(name="alice", uid=4242, gid=4243, home=tmp_path)
    assert ensure_owned_dir(root, target) is False
    assert calls == []


def test_ensure_owned_dir_chowns_intermediate_parents(tmp_path, monkeypatch):
    """If parents are created on the way down, they belong to the
    student too — otherwise the student git-cloning the tree owns only
    the leaves, not the wrapper directory."""
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(_fsutil.os, "chown", lambda p, u, g: calls.append((str(p), u, g)))

    target = TargetUser(name="alice", uid=4242, gid=4243, home=tmp_path)
    root = tmp_path / "outer" / "inner" / "store"
    assert ensure_owned_dir(root, target) is True

    chowned_paths = {c[0] for c in calls}
    assert str(tmp_path / "outer") in chowned_paths
    assert str(tmp_path / "outer" / "inner") in chowned_paths
    assert str(root) in chowned_paths


def test_ensure_owned_dir_target_user_none_still_creates(tmp_path: Path):
    root = tmp_path / "store"
    assert ensure_owned_dir(root, None) is True
    assert root.is_dir()


# ---------- write_owned ----------


def test_write_owned_atomic_no_temp_left(tmp_path: Path):
    path = tmp_path / "f"
    write_owned(path, b"hello\n", _self_user())
    assert path.read_bytes() == b"hello\n"
    assert not path.with_suffix(".tmp").exists()


def test_write_owned_sets_mode(tmp_path: Path):
    path = tmp_path / "f"
    write_owned(path, b"x", _self_user(), file_mode=0o600)
    assert oct(path.stat().st_mode & 0o7777) == "0o600"


def test_write_owned_replaces_existing(tmp_path: Path):
    path = tmp_path / "f"
    path.write_bytes(b"old")
    write_owned(path, b"new", _self_user())
    assert path.read_bytes() == b"new"


def test_write_owned_target_user_none_writes_anyway(tmp_path: Path):
    path = tmp_path / "f"
    write_owned(path, b"x", None)
    assert path.read_bytes() == b"x"
