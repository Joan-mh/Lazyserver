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

from lazyserver.backup._fsutil import mkdir_owned_chain, write_owned
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
